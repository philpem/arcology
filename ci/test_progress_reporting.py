"""Progress-reporting and heartbeat-based stale detection tests.

Covers the live-progress columns added to Analysis and the machinery built on
them:

  - PUT /api/analysis/<id> accepts the worker's progress_* fields, stamps
    progress_updated_at server-side, and clears them on completion.
  - A bare heartbeat refreshes progress_updated_at.
  - reset-stale uses COALESCE(progress_updated_at, started_at): an actively
    heartbeating job is not reset; a truly idle one is (and its progress_*
    columns are cleared).
  - enumerate_extracted_files() / post_file_records() invoke their optional
    progress_callback with (done, total) and behave unchanged when it's None.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_progress_reporting -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-progress-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
_WORKER_KEY = os.environ['WORKER_API_KEY']


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _db.create_all()
    return app, _db


class _ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def _running_analysis(self, suffix, *, started_at=None, progress_updated_at=None):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item, StorageDirectory

        item = Item(name=f'Progress Item {suffix}')
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id, label=f'A-{suffix}', artefact_type=ArtefactType.ISO,
            original_filename=f'{suffix}.iso', storage_path=f'{suffix}.iso',
            storage_directory=StorageDirectory.UPLOADS,
        )
        self.db.session.add(art)
        self.db.session.flush()
        a = Analysis(
            artefact_id=art.id,
            analysis_type=AnalysisType.FILE_EXTRACTION,
            status=AnalysisStatus.RUNNING,
            started_at=started_at or datetime.utcnow(),
            progress_updated_at=progress_updated_at,
        )
        self.db.session.add(a)
        self.db.session.commit()
        return a

    def _worker_headers(self):
        return {'X-API-Key': _WORKER_KEY, 'Content-Type': 'application/json'}


class TestProgressUpdate(_ApiBase):
    def test_progress_update_stamps_and_completion_clears(self):
        from myapp.database import Analysis

        with self.app.app_context():
            a = self._running_analysis('upd')
            aid = a.id
            self.assertIsNone(a.progress_updated_at)

            resp = self.client.put(
                f'/api/analysis/{aid}',
                json={'progress_message': 'Hashing extracted files',
                      'progress_current': 7, 'progress_total': 20},
                headers=self._worker_headers(),
            )
            self.assertEqual(resp.status_code, 200)

            a = self.db.session.get(Analysis, aid)
            self.assertEqual(a.progress_message, 'Hashing extracted files')
            self.assertEqual(a.progress_current, 7)
            self.assertEqual(a.progress_total, 20)
            self.assertIsNotNone(a.progress_updated_at)  # server-stamped

            # Completion clears the live-progress fields.
            resp = self.client.put(
                f'/api/analysis/{aid}',
                json={'status': 'completed', 'success': True, 'summary': 'done'},
                headers=self._worker_headers(),
            )
            self.assertEqual(resp.status_code, 200)
            a = self.db.session.get(Analysis, aid)
            self.assertIsNone(a.progress_message)
            self.assertIsNone(a.progress_current)
            self.assertIsNone(a.progress_total)
            self.assertIsNone(a.progress_updated_at)
            self.assertEqual(a.summary, 'done')  # final result preserved

    def test_bare_heartbeat_stamps_timestamp(self):
        from myapp.database import Analysis

        with self.app.app_context():
            a = self._running_analysis('hb')
            aid = a.id
            resp = self.client.put(
                f'/api/analysis/{aid}',
                json={'heartbeat': True},
                headers=self._worker_headers(),
            )
            self.assertEqual(resp.status_code, 200)
            a = self.db.session.get(Analysis, aid)
            self.assertIsNotNone(a.progress_updated_at)

    def test_progress_fields_require_auth(self):
        with self.app.app_context():
            a = self._running_analysis('noauth')
            aid = a.id
        resp = self.client.put(
            f'/api/analysis/{aid}',
            json={'progress_message': 'sneaky'},
            headers={'Content-Type': 'application/json'},
        )
        self.assertIn(resp.status_code, (401, 403))


class TestStaleHeartbeat(_ApiBase):
    def test_reset_stale_respects_heartbeat(self):
        from myapp.database import Analysis, AnalysisStatus

        with self.app.app_context():
            old = datetime.utcnow() - timedelta(hours=2)
            # Idle: started long ago, never reported progress -> stale.
            idle = self._running_analysis('idle', started_at=old)
            # Alive: started long ago but heartbeated just now -> not stale.
            alive = self._running_analysis(
                'alive', started_at=old, progress_updated_at=datetime.utcnow())
            alive.progress_message = 'Hashing extracted files'
            alive.progress_current = 5
            alive.progress_total = 9
            self.db.session.commit()
            idle_id, alive_id = idle.id, alive.id

            resp = self.client.post('/api/analysis/reset-stale', json={},
                                    headers=self._worker_headers())
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()['reset'], 1)

            idle = self.db.session.get(Analysis, idle_id)
            alive = self.db.session.get(Analysis, alive_id)
            # Idle job requeued and wiped clean.
            self.assertEqual(idle.status, AnalysisStatus.PENDING)
            self.assertIsNone(idle.started_at)
            self.assertIsNone(idle.progress_updated_at)
            # Heartbeating job untouched.
            self.assertEqual(alive.status, AnalysisStatus.RUNNING)
            self.assertEqual(alive.progress_current, 5)


class TestEnumerateProgressCallback(unittest.TestCase):
    def _tree(self):
        d = Path(tempfile.mkdtemp(prefix='arco-enum-'))
        (d / 'a.txt').write_bytes(b'hello')
        (d / 'sub').mkdir()
        (d / 'sub' / 'b.txt').write_bytes(b'world')
        return d

    def test_callback_receives_done_total(self):
        from worker.arcworker.tools.extraction import enumerate_extracted_files

        calls = []
        files = enumerate_extracted_files(
            self._tree(),
            progress_callback=lambda done, total: calls.append((done, total)),
        )
        self.assertEqual(len([f for f in files if not f.get('is_directory')]), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], (2, 2))

    def test_no_callback_is_unchanged(self):
        from worker.arcworker.tools.extraction import enumerate_extracted_files

        files = enumerate_extracted_files(self._tree())
        self.assertEqual(len([f for f in files if not f.get('is_directory')]), 2)


class TestPostFileRecordsProgressCallback(unittest.TestCase):
    def test_callback_per_batch(self):
        from worker.arcworker.api import ArcologyAPI

        api = ArcologyAPI('http://example.invalid', Path('/tmp'), Path('/tmp'))
        # Echo back an id per posted file so post_file_records can build its
        # path→id map (the contract callers rely on for archive detection).
        api.post = lambda endpoint, data: {
            'ok': True,
            'files': [{'id': int(f['path'][1:]), 'path': f['path']}
                      for f in data['files']],
        }

        calls = []
        files = [{'path': f'f{i}'} for i in range(250)]
        path_to_id = api.post_file_records(
            'puuid', files, batch_size=100,
            progress_callback=lambda posted, tot: calls.append((posted, tot)),
        )
        self.assertEqual(len(path_to_id), 250)
        self.assertEqual(path_to_id['f100'], 100)
        self.assertEqual(calls, [(100, 250), (200, 250), (250, 250)])


class TestMonitorHeartbeatCap(unittest.TestCase):
    """The cancellation monitor heartbeats only while under HEARTBEAT_MAX_SECONDS.

    Past the cap, process-liveness alone must not keep a wedged (non-progressing)
    job fresh forever — the heartbeat stops so stale reset can recover it.
    """

    def _run_one_iteration(self, cap):
        from worker.arcworker import analysis as analysis_mod
        from worker.arcworker.analysis import AnalysisWorker

        fake_self = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'status': 'running'}
        fake_self.api._request_response.return_value = resp

        stop_event = MagicMock()
        stop_event.wait.side_effect = [False, True]  # exactly one loop iteration
        stop_event.is_set.return_value = False

        with patch.object(analysis_mod, 'HEARTBEAT_MAX_SECONDS', cap):
            AnalysisWorker._monitor_cancellation(fake_self, 'uuid', 7, stop_event)
        return fake_self

    def test_heartbeat_sent_within_cap(self):
        fake_self = self._run_one_iteration(cap=9999)
        fake_self.report_progress.assert_called_once_with(7, heartbeat=True)

    def test_heartbeat_suppressed_past_cap(self):
        fake_self = self._run_one_iteration(cap=0)
        fake_self.report_progress.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
