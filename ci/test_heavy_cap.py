"""
Heavy-job fairness cap and worker-heartbeat registry.

Covers resolve_heavy_cap() across the absolute / percentage / keep-N-free /
disabled forms, the worker-heartbeat registry that backs the live worker count
(record/active-count/GC), and the fairness filter inside
pending_claimable_query() (cap met withholds heavy, below cap admits, nothing
running admits, light never filtered, taskrunner opt-out).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_heavy_cap -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-heavy-cap-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']

# A representative heavy type and a light type for the filter tests.
def _types():
    from arcology_shared.enums import HEAVY_ANALYSIS_TYPES, AnalysisType
    heavy = AnalysisType.FILE_EXTRACTION
    assert heavy in HEAVY_ANALYSIS_TYPES
    light = AnalysisType.CHECKSUM_COMPUTE
    assert light not in HEAVY_ANALYSIS_TYPES
    return heavy, light


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def setUp(self):
        from myapp.database import Analysis, Artefact, Item, WorkerHeartbeat
        self._ctx = self.app.app_context()
        self._ctx.push()
        Analysis.query.delete()
        Artefact.query.delete()
        Item.query.delete()
        WorkerHeartbeat.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        # Restore default cap between tests.
        self.app.config.pop('ANALYSIS_HEAVY_RUNNING_CAP', None)
        self._ctx.pop()

    def _make_artefact(self, label='a'):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        item = Item(name=f'Item {label}')
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id, label=label, artefact_type=ArtefactType.SCP,
            original_filename=f'{label}.scp', storage_path=f'{label}.scp',
            storage_directory=StorageDirectory.UPLOADS,
        )
        self.db.session.add(art)
        self.db.session.flush()
        return art

    def _add(self, analysis_type, status, artefact_id, priority=0):
        from myapp.database import Analysis
        a = Analysis(artefact_id=artefact_id, analysis_type=analysis_type,
                     status=status, priority=priority)
        self.db.session.add(a)
        self.db.session.flush()
        return a

    def _seed_workers(self, n):
        from myapp.services.analysis_queue import record_worker_heartbeat
        for i in range(n):
            record_worker_heartbeat(f'worker-{i}')


class TestResolveHeavyCap(_Base):
    def _resolve(self, value):
        from myapp.services.analysis_queue import resolve_heavy_cap
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = value
        return resolve_heavy_cap()

    def test_absolute_needs_no_workers(self):
        # No heartbeats at all: an absolute cap still resolves.
        self.assertEqual(self._resolve(3), 3)

    def test_zero_disables(self):
        self.assertIsNone(self._resolve(0))

    def test_empty_string_disables(self):
        self.assertIsNone(self._resolve(''))

    def test_percentage_of_live_workers(self):
        self._seed_workers(4)
        self.assertEqual(self._resolve('50%'), 2)

    def test_percentage_empty_registry_no_throttle(self):
        # Relative form with zero known workers -> nothing running -> no throttle.
        self.assertIsNone(self._resolve('50%'))

    def test_keep_n_free(self):
        self._seed_workers(3)
        self.assertEqual(self._resolve(-1), 2)   # keep 1 of 3 free

    def test_keep_n_free_empty_registry_no_throttle(self):
        self.assertIsNone(self._resolve(-1))

    def test_cap_at_or_above_worker_count_no_throttle(self):
        self._seed_workers(2)
        # Absolute cap 3 with only 2 workers -> can't run 3 heavy anyway.
        self.assertIsNone(self._resolve(3))

    def test_keep_n_free_clamped_to_one(self):
        self._seed_workers(1)
        # workers - 1 == 0 -> clamped to >= 1, but 1 >= workers(1) -> no throttle.
        self.assertIsNone(self._resolve(-1))


class TestWorkerHeartbeatRegistry(_Base):
    def test_upsert_single_row_per_id(self):
        from myapp.database import WorkerHeartbeat
        from myapp.services.analysis_queue import record_worker_heartbeat
        record_worker_heartbeat('w1')
        record_worker_heartbeat('w1')
        self.assertEqual(WorkerHeartbeat.query.count(), 1)

    def test_rate_limited_skip_does_not_rewrite(self):
        from myapp.database import WorkerHeartbeat
        from myapp.services.analysis_queue import record_worker_heartbeat
        record_worker_heartbeat('rl')
        first_seen = self.db.session.get(WorkerHeartbeat, 'rl').last_seen
        # A second call within the min write interval is skipped: last_seen
        # stays put rather than churning a write/commit on every poll.
        record_worker_heartbeat('rl')
        self.assertEqual(self.db.session.get(WorkerHeartbeat, 'rl').last_seen, first_seen)

    def test_record_is_best_effort_on_error(self):
        # A failing heartbeat write must never propagate (it would otherwise 500
        # the worker poll / status update).  Force a commit failure and confirm
        # the call swallows it.
        from unittest import mock
        from myapp.extensions import db
        from myapp.services.analysis_queue import record_worker_heartbeat
        with mock.patch.object(db.session, 'commit', side_effect=RuntimeError('boom')):
            record_worker_heartbeat('err-worker')  # must not raise

    def test_active_count_honours_window(self):
        from myapp.database import WorkerHeartbeat
        from myapp.services.analysis_queue import active_worker_count
        self._seed_workers(2)
        self.assertEqual(active_worker_count(), 2)
        # Age one row beyond the freshness window.
        self.app.config['ANALYSIS_WORKER_HEARTBEAT_WINDOW'] = 60
        stale = self.db.session.get(WorkerHeartbeat, 'worker-0')
        stale.last_seen = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120)
        self.db.session.commit()
        self.assertEqual(active_worker_count(), 1)
        self.app.config.pop('ANALYSIS_WORKER_HEARTBEAT_WINDOW', None)

    def test_gc_deletes_only_aged_rows(self):
        from myapp.database import WorkerHeartbeat
        from myapp.services.analysis_queue import gc_stale_worker_heartbeats
        self.app.config['ANALYSIS_WORKER_HEARTBEAT_WINDOW'] = 60
        self._seed_workers(2)
        aged = self.db.session.get(WorkerHeartbeat, 'worker-0')
        aged.last_seen = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60 * 20)
        self.db.session.commit()
        deleted = gc_stale_worker_heartbeats(multiple=10)
        self.assertEqual(deleted, 1)
        self.assertEqual(WorkerHeartbeat.query.count(), 1)
        self.assertIsNotNone(self.db.session.get(WorkerHeartbeat, 'worker-1'))
        self.app.config.pop('ANALYSIS_WORKER_HEARTBEAT_WINDOW', None)

    def test_poll_records_heartbeat(self):
        from myapp.database import WorkerHeartbeat
        resp = self.client.get('/api/analysis/pending',
                               headers={'X-API-Key': _WORKER_KEY,
                                        'X-Worker-Id': 'poll-worker'})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self.db.session.get(WorkerHeartbeat, 'poll-worker'))


class TestFairnessFilter(_Base):
    def _claimable(self, **kwargs):
        from myapp.services.analysis_queue import pending_claimable_query
        return {a.uuid for a in pending_claimable_query(**kwargs).all()}

    def test_cap_met_withholds_heavy_admits_light(self):
        from arcology_shared.enums import AnalysisStatus
        heavy, light = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 1   # absolute, no workers needed
        self._add(heavy, AnalysisStatus.RUNNING, art.id)
        pending_heavy = self._add(heavy, AnalysisStatus.PENDING, art.id)
        pending_light = self._add(light, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()
        claimable = self._claimable()
        self.assertNotIn(pending_heavy.uuid, claimable)
        self.assertIn(pending_light.uuid, claimable)

    def test_below_cap_admits_heavy(self):
        from arcology_shared.enums import AnalysisStatus
        heavy, _ = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 2
        self._add(heavy, AnalysisStatus.RUNNING, art.id)
        pending_heavy = self._add(heavy, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()
        self.assertIn(pending_heavy.uuid, self._claimable())

    def test_nothing_running_admits_heavy(self):
        from arcology_shared.enums import AnalysisStatus
        heavy, _ = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 1
        h1 = self._add(heavy, AnalysisStatus.PENDING, art.id)
        h2 = self._add(heavy, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()
        claimable = self._claimable()
        # With nothing running, the queue must not be fully blocked.
        self.assertTrue({h1.uuid, h2.uuid} & claimable)

    def test_disabled_no_filter(self):
        from arcology_shared.enums import AnalysisStatus
        heavy, _ = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 0
        self._add(heavy, AnalysisStatus.RUNNING, art.id)
        pending_heavy = self._add(heavy, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()
        self.assertIn(pending_heavy.uuid, self._claimable())

    def test_taskrunner_opt_out_ignores_cap(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        heavy, _ = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 1
        self._add(heavy, AnalysisStatus.RUNNING, art.id)
        # A control-plane heavy that the taskrunner owns must still be claimable.
        item_delete = self._add(AnalysisType.ITEM_DELETE, AnalysisStatus.PENDING, None)
        self.db.session.commit()
        self.assertIn(item_delete.uuid, self._claimable(apply_heavy_cap=False))

    def test_running_control_plane_job_does_not_consume_worker_budget(self):
        # A RUNNING control-plane job (taskrunner-owned) must not count toward
        # the worker fleet's heavy budget, so a pending worker heavy job is still
        # claimable even when the control-plane job is running.
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        heavy, _ = _types()
        art = self._make_artefact()
        self.app.config['ANALYSIS_HEAVY_RUNNING_CAP'] = 1
        self._add(AnalysisType.ITEM_DELETE, AnalysisStatus.RUNNING, None)
        pending_heavy = self._add(heavy, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()
        # running_heavy excludes ITEM_DELETE, so the worker heavy job is admitted.
        self.assertIn(pending_heavy.uuid, self._claimable())


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
