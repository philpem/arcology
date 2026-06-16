"""
Re-analysis dispatch-barrier tests for GET /api/analysis/pending.

A re-analysis queues a CLEANUP job (delete the previous run's output) alongside
the replacement analyses.  The worker-poll endpoint must not hand out any other
analysis for an artefact while that artefact's CLEANUP is still outstanding
(PENDING or RUNNING) — otherwise a second worker could run a replacement job
concurrently and the CLEANUP would then delete its output (notably the shared
outputs/.cache/<uuid> partition cache).  The CLEANUP itself is never blocked,
item-deletion cleanups (artefact_id IS NULL) gate nothing, and a terminal
CLEANUP lifts the barrier.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_queue_barrier -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-queue-barrier-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestReanalysisDispatchBarrier(unittest.TestCase):
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
        # Each test starts from an empty analyses/artefacts/items set.
        from myapp.database import Analysis, Artefact, Item
        self._ctx = self.app.app_context()
        self._ctx.push()
        Analysis.query.delete()
        Artefact.query.delete()
        Item.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self._ctx.pop()

    def _make_artefact(self, label):
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

    def _add_analysis(self, analysis_type, status, artefact_id, priority=0):
        from myapp.database import Analysis
        a = Analysis(artefact_id=artefact_id, analysis_type=analysis_type,
                     status=status, priority=priority)
        self.db.session.add(a)
        self.db.session.flush()
        return a

    def _poll_uuids(self):
        resp = self.client.get('/api/analysis/pending',
                               headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200)
        return {a['uuid'] for a in resp.get_json()['analyses']}

    def test_outstanding_cleanup_blocks_same_artefact_jobs(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        art = self._make_artefact('blocked')
        cleanup = self._add_analysis(
            AnalysisType.CLEANUP, AnalysisStatus.PENDING, art.id, priority=11)
        checksum = self._add_analysis(
            AnalysisType.CHECKSUM_COMPUTE, AnalysisStatus.PENDING, art.id, priority=10)
        self.db.session.commit()

        uuids = self._poll_uuids()
        # The cleanup is dispatched; the replacement job is held back.
        self.assertIn(cleanup.uuid, uuids)
        self.assertNotIn(checksum.uuid, uuids)

    def test_running_cleanup_also_blocks(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        art = self._make_artefact('running')
        self._add_analysis(AnalysisType.CLEANUP, AnalysisStatus.RUNNING, art.id)
        checksum = self._add_analysis(
            AnalysisType.CHECKSUM_COMPUTE, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()

        self.assertNotIn(checksum.uuid, self._poll_uuids())

    def test_terminal_cleanup_lifts_barrier(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        for terminal in (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED):
            with self.subTest(terminal=terminal):
                from myapp.database import Analysis, Artefact, Item
                Analysis.query.delete()
                Artefact.query.delete()
                Item.query.delete()
                self.db.session.commit()

                art = self._make_artefact('lifted')
                self._add_analysis(AnalysisType.CLEANUP, terminal, art.id)
                checksum = self._add_analysis(
                    AnalysisType.CHECKSUM_COMPUTE, AnalysisStatus.PENDING, art.id)
                self.db.session.commit()

                self.assertIn(checksum.uuid, self._poll_uuids())

    def test_cleanup_does_not_block_other_artefacts(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        blocked = self._make_artefact('blocked')
        other = self._make_artefact('other')
        self._add_analysis(AnalysisType.CLEANUP, AnalysisStatus.PENDING, blocked.id)
        other_job = self._add_analysis(
            AnalysisType.CHECKSUM_COMPUTE, AnalysisStatus.PENDING, other.id)
        self.db.session.commit()

        self.assertIn(other_job.uuid, self._poll_uuids())

    def test_item_deletion_cleanup_gates_nothing(self):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        # Item-deletion cleanups carry no artefact_id; they must not block jobs.
        self._add_analysis(AnalysisType.CLEANUP, AnalysisStatus.PENDING, None)
        art = self._make_artefact('unaffected')
        job = self._add_analysis(
            AnalysisType.CHECKSUM_COMPUTE, AnalysisStatus.PENDING, art.id)
        self.db.session.commit()

        self.assertIn(job.uuid, self._poll_uuids())


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
