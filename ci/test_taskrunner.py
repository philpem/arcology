"""
Tests for the in-process task runner that owns the DB-only control-plane
analyses (myapp/taskrunner), and the worker hard-split that hands those job
types to it instead of the analysis worker.

Covers:
  * CONTROL_PLANE_ANALYSIS_TYPES is exactly the five DB-only types, and the
    DISPATCH table covers exactly that set;
  * the worker's effective type filter excludes the control-plane types, and the
    worker-poll endpoint never returns them when given that filter;
  * the taskrunner's atomic claim: exactly one of two concurrent claims wins;
  * the CLEANUP re-analysis barrier blocks a control-plane claim (shared with
    the worker poll via pending_claimable_query);
  * the run-to-completion job drivers (link / delete / recognition) reach the
    correct end state in one deadline-free call;
  * the periodic interval gate fires only after its interval elapses.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_taskrunner -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-taskrunner-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestControlPlaneClassification(unittest.TestCase):
    """The shared frozenset and the dispatch table must stay in lockstep."""

    def test_control_plane_set_is_the_five_db_only_types(self):
        from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES, AnalysisType
        self.assertEqual(
            CONTROL_PLANE_ANALYSIS_TYPES,
            frozenset({
                AnalysisType.HASH_RESCAN,
                AnalysisType.PRODUCT_RECOGNITION,
                AnalysisType.HASHDB_LINK,
                AnalysisType.HASHDB_DELETE,
                AnalysisType.HASHDB_RECOGNITION,
            }),
        )

    def test_dispatch_covers_exactly_control_plane(self):
        from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES
        from myapp.taskrunner.dispatch import DISPATCH
        self.assertEqual(set(DISPATCH), set(CONTROL_PLANE_ANALYSIS_TYPES))

    def test_worker_does_not_register_control_plane_handlers(self):
        import worker.arcworker.analyses as analyses
        from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES
        registered = set(analyses.HANDLERS)  # keyed by AnalysisType.value
        leaked = {t.value for t in CONTROL_PLANE_ANALYSIS_TYPES} & registered
        self.assertEqual(leaked, set())


class TestWorkerExcludesControlPlane(unittest.TestCase):
    """The worker's effective filter must drop control-plane types, and the
    worker-poll endpoint must return none of them under that filter."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            Artefact,
            Item,
            StorageDirectory,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()
            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            cls.artefact_id = art.id
            # One data-plane and one control-plane PENDING job.
            db.session.add(Analysis(artefact_id=art.id,
                                    analysis_type=AnalysisType.METADATA_EXTRACT,
                                    status=AnalysisStatus.PENDING))
            db.session.add(Analysis(artefact_id=art.id,
                                    analysis_type=AnalysisType.HASH_RESCAN,
                                    status=AnalysisStatus.PENDING))
            db.session.commit()

    def test_effective_types_excludes_control_plane(self):
        from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES, AnalysisType
        from worker.arcworker.analysis import AnalysisWorker

        # Build a worker without going through __init__'s storage/API setup.
        worker = AnalysisWorker.__new__(AnalysisWorker)
        worker.analysis_types = []  # no WORKER_ANALYSIS_TYPES opt-in => all types
        worker._control_plane_names = frozenset(
            t.name for t in CONTROL_PLANE_ANALYSIS_TYPES)

        effective = set(worker._effective_types())
        self.assertNotIn(AnalysisType.HASH_RESCAN.name, effective)
        self.assertNotIn(AnalysisType.HASHDB_LINK.name, effective)
        # A data-plane type is still present.
        self.assertIn(AnalysisType.METADATA_EXTRACT.name, effective)

    def test_pending_poll_with_worker_filter_omits_control_plane(self):
        from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES, AnalysisType

        worker_types = [t.name for t in AnalysisType
                        if t not in CONTROL_PLANE_ANALYSIS_TYPES]
        resp = self.client.get(
            '/api/analysis/pending',
            query_string={'types': ','.join(worker_types)},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        returned = {a['analysis_type'] for a in resp.get_json()['analyses']}
        self.assertNotIn(AnalysisType.HASH_RESCAN.value, returned)
        self.assertIn(AnalysisType.METADATA_EXTRACT.value, returned)


class TestTaskRunnerClaim(unittest.TestCase):
    """Atomic claim + CLEANUP-barrier eligibility for the taskrunner."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.db = db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        self.artefact_id = art.id
        db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _runner(self):
        from myapp.taskrunner.runner import TaskRunner
        return TaskRunner(self.app)

    def _add_hash_rescan(self):
        from myapp.database import Analysis, AnalysisStatus, AnalysisType
        a = Analysis(artefact_id=self.artefact_id,
                     analysis_type=AnalysisType.HASH_RESCAN,
                     status=AnalysisStatus.PENDING)
        self.db.session.add(a)
        self.db.session.commit()
        return a.id

    def test_single_winner_on_concurrent_claim(self):
        from myapp.database import Analysis, AnalysisStatus

        analysis_id = self._add_hash_rescan()
        r1, r2 = self._runner(), self._runner()
        first = r1._claim_one()
        second = r2._claim_one()
        # Exactly one runner wins the row.
        self.assertEqual({first is not None, second is not None}, {True, False})
        winner = first or second
        self.assertEqual(winner.id, analysis_id)
        self.assertEqual(self.db.session.get(Analysis, analysis_id).status,
                         AnalysisStatus.RUNNING)

    def test_cleanup_barrier_blocks_control_plane_claim(self):
        from myapp.database import Analysis, AnalysisStatus, AnalysisType

        self._add_hash_rescan()
        # A PENDING CLEANUP for the same artefact must block its other jobs.
        self.db.session.add(Analysis(artefact_id=self.artefact_id,
                                     analysis_type=AnalysisType.CLEANUP,
                                     status=AnalysisStatus.PENDING))
        self.db.session.commit()
        # CLEANUP is data-plane (worker-owned), so the taskrunner claim sees no
        # eligible control-plane job for this artefact.
        self.assertIsNone(self._runner()._claim_one())

    def test_complete_records_summary(self):
        from myapp.database import Analysis, AnalysisStatus

        analysis_id = self._add_hash_rescan()
        runner = self._runner()
        runner._claim_one()
        runner._complete(analysis_id, {'summary': 'done', 'updated': 0})
        row = self.db.session.get(Analysis, analysis_id)
        self.assertEqual(row.status, AnalysisStatus.COMPLETED)
        self.assertTrue(row.success)
        self.assertEqual(row.summary, 'done')

    def test_complete_tolerates_non_serializable_result(self):
        # _complete must not raise on a result dict carrying a non-JSON value
        # (json.dumps(..., default=str)) — otherwise the job is left RUNNING and
        # re-run on every stale-reset.
        import json
        from datetime import datetime, timezone
        from myapp.database import Analysis, AnalysisStatus

        analysis_id = self._add_hash_rescan()
        runner = self._runner()
        runner._claim_one()
        runner._complete(analysis_id,
                         {'summary': 'ok', 'when': datetime.now(timezone.utc)})
        row = self.db.session.get(Analysis, analysis_id)
        self.assertEqual(row.status, AnalysisStatus.COMPLETED)
        # details stored as valid JSON with the datetime stringified.
        self.assertEqual(json.loads(row.details)['summary'], 'ok')

    def test_complete_does_not_overwrite_a_cancel(self):
        from myapp.database import Analysis, AnalysisStatus

        analysis_id = self._add_hash_rescan()
        runner = self._runner()
        runner._claim_one()
        # Simulate a UI/CLI cancel landing mid-job.
        self.db.session.get(Analysis, analysis_id).status = AnalysisStatus.FAILED
        self.db.session.commit()
        runner._complete(analysis_id, {'summary': 'late'})
        self.assertEqual(self.db.session.get(Analysis, analysis_id).status,
                         AnalysisStatus.FAILED)

    def test_periodic_interval_gate(self):
        import time as _time

        runner = self._runner()
        calls = {'n': 0}
        runner._periodics = [(100, lambda: calls.__setitem__('n', calls['n'] + 1), 'x')]
        runner._run_periodics()              # first run fires
        runner._run_periodics()              # within interval: suppressed
        self.assertEqual(calls['n'], 1)
        runner._periodic_last['x'] = _time.monotonic() - 200  # age it past 100s
        runner._run_periodics()              # now fires again
        self.assertEqual(calls['n'], 2)


class TestRunToCompletionJobs(unittest.TestCase):
    """The deadline-free run_*_job drivers reach the right end state."""

    def setUp(self):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            StorageDirectory,
        )
        from myapp.extensions import db

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.db = db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.hdb = HashDatabase(name='DB', enable_product_recognition=True)
        db.session.add(self.hdb)
        db.session.flush()

        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        self.part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                              filesystem=FilesystemType.DFS)
        db.session.add(self.part)
        db.session.flush()

        # Three products, each one required file present in its own folder.
        for i in range(3):
            md5 = f'{i:032x}'
            prod = KnownProduct(database_id=self.hdb.id, title=f'!App{i}')
            db.session.add(prod)
            db.session.flush()
            db.session.add(KnownFile(database_id=self.hdb.id, product_id=prod.id,
                                     filename='!Run', md5=md5, is_required=True))
            db.session.add(ExtractedFile(partition_id=self.part.id,
                                         path=f'App{i}/!Run', filename='!Run',
                                         md5=md5, is_directory=False))
        db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def test_link_job_links_all_matching_files(self):
        from myapp.database import ExtractedFile
        from myapp.services.hashdb_jobs import run_hashdb_link_job

        result = run_hashdb_link_job(self.hdb.id)
        self.assertEqual(result['processed'], 3)  # three known files checked
        linked = ExtractedFile.query.filter(
            ExtractedFile.known_file_id.isnot(None)).count()
        self.assertEqual(linked, 3)

    def test_recognition_job_finds_all_products(self):
        from myapp.database import RecognisedProduct
        from myapp.services.hashdb_jobs import run_hashdb_recognition_job

        result = run_hashdb_recognition_job(self.hdb.id)
        self.assertEqual(result['processed'], 3)
        self.assertEqual(RecognisedProduct.query.count(), 3)

    def test_delete_job_removes_database_and_children(self):
        from myapp.database import HashDatabase, KnownFile, KnownProduct
        from myapp.services.hashdb_jobs import run_hashdb_delete_job

        run_hashdb_delete_job(self.hdb.id)
        self.assertIsNone(self.db.session.get(HashDatabase, self.hdb.id))
        self.assertEqual(KnownFile.query.filter_by(database_id=self.hdb.id).count(), 0)
        self.assertEqual(KnownProduct.query.filter_by(database_id=self.hdb.id).count(), 0)

    def test_partition_recognition_job(self):
        from myapp.database import RecognisedProduct
        from myapp.services.hashdb_jobs import run_partition_recognition_job

        result = run_partition_recognition_job(self.part)
        self.assertEqual(result['processed'], 3)
        self.assertEqual(RecognisedProduct.query.count(), 3)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
