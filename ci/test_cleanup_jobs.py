"""
CLEANUP job queue tests.

Covers the replacement of the fire-and-forget daemon-thread file cleanup
with worker CLEANUP analysis jobs:

  - queue_storage_cleanup(): creates a PENDING CLEANUP analysis carrying
    storage keys in hints; no-op when there is nothing to delete.
  - collect_output_cleanup_keys(): collects output keys for an artefact
    tree and never includes the uploaded originals.
  - bulk_delete_item(): queues a CLEANUP job (with artefact_id NULL) in
    the same transaction as the deletes.
  - worker process_cleanup handler: deletes keys/prefixes via the storage
    backend, tolerates missing keys, reports counts.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_cleanup_jobs -v
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-cleanup-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _db.create_all()
    return app, _db


class _CleanupBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def _make_item_tree(self, suffix):
        """Item with one artefact, one completed analysis with output_path."""
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item, StorageDirectory
        from shared.enums import AnalysisType, ArtefactType

        item = Item(name=f'Cleanup Item {suffix}')
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id, label=f'A-{suffix}', artefact_type=ArtefactType.HFE,
            original_filename=f'{suffix}.hfe', storage_path=f'{suffix}.hfe',
            storage_directory=StorageDirectory.UPLOADS,
        )
        self.db.session.add(art)
        self.db.session.flush()
        self.db.session.add(Analysis(
            artefact_id=art.id,
            analysis_type=AnalysisType.FLUX_DECODE,
            status=AnalysisStatus.COMPLETED,
            output_path=f'itempart/{art.uuid}_slug/flux_decode',
            details=json.dumps({'outputs': [{'filename': f'{suffix}_vis.png'}]}),
        ))
        self.db.session.commit()
        return item, art


class TestQueueStorageCleanup(_CleanupBase):
    def test_creates_pending_cleanup_job_with_hints(self):
        from myapp.database import Analysis, AnalysisStatus
        from myapp.services.artefact_lifecycle import queue_storage_cleanup
        from shared.enums import AnalysisType

        with self.app.app_context():
            keys = {
                'artefact_keys': ['uploads/x.img'],
                'output_file_keys': ['outputs/vis.png'],
                'output_dir_prefixes': ['outputs/item/art/'],
                'cache_prefixes': ['outputs/.cache/abcd'],
            }
            job = queue_storage_cleanup(keys, commit=True)
            self.assertIsNotNone(job)
            self.assertIsNone(job.artefact_id)
            self.assertEqual(job.analysis_type, AnalysisType.CLEANUP)
            self.assertEqual(job.status, AnalysisStatus.PENDING)
            self.assertEqual(json.loads(job.hints), keys)
            # row really committed
            self.assertIsNotNone(self.db.session.get(Analysis, job.id))

    def test_noop_when_nothing_to_delete(self):
        from myapp.services.artefact_lifecycle import queue_storage_cleanup

        with self.app.app_context():
            empty = {'artefact_keys': [], 'output_file_keys': [],
                     'output_dir_prefixes': [], 'cache_prefixes': []}
            self.assertIsNone(queue_storage_cleanup(empty, commit=True))


class TestCollectOutputCleanupKeys(_CleanupBase):
    def test_collects_outputs_but_never_uploads(self):
        from myapp.services.artefact_lifecycle import collect_output_cleanup_keys

        with self.app.app_context():
            _, art = self._make_item_tree('collect')
            keys = collect_output_cleanup_keys(art)

            # Re-analyse must never delete the uploaded originals.
            self.assertEqual(keys['artefact_keys'], [])
            self.assertIn(f'outputs/itempart/{art.uuid}_slug/flux_decode',
                          keys['output_dir_prefixes'])
            self.assertIn('outputs/collect_vis.png', keys['output_file_keys'])
            self.assertIn(f'outputs/.cache/{art.uuid}', keys['cache_prefixes'])


class TestBulkDeleteItemQueuesCleanup(_CleanupBase):
    def test_bulk_delete_queues_cleanup_job(self):
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item
        from myapp.services.artefact_lifecycle import bulk_delete_item
        from shared.enums import AnalysisType

        with self.app.app_context():
            item, art = self._make_item_tree('bulkdel')
            item_id, art_id, art_uuid = item.id, art.id, art.uuid

            bulk_delete_item(item)

            # Item and artefact gone
            self.assertIsNone(self.db.session.get(Item, item_id))
            self.assertIsNone(self.db.session.get(Artefact, art_id))

            # One CLEANUP job queued, artefact-less, carrying the storage keys
            job = Analysis.query.filter(
                Analysis.analysis_type == AnalysisType.CLEANUP
            ).order_by(Analysis.id.desc()).first()
            self.assertIsNotNone(job)
            self.assertIsNone(job.artefact_id)
            self.assertEqual(job.status, AnalysisStatus.PENDING)
            hints = json.loads(job.hints)
            self.assertIn('uploads/bulkdel.hfe', hints['artefact_keys'])
            self.assertIn(f'outputs/.cache/{art_uuid}', hints['cache_prefixes'])
            self.assertIn('outputs/bulkdel_vis.png', hints['output_file_keys'])


class TestWorkerProcessCleanup(unittest.TestCase):
    """Worker-side handler, exercised with a mocked AnalysisWorker."""

    def _run(self, hints):
        from worker.arcworker.analyses.cleanup import process_cleanup
        from worker.arcworker.analysis import AnalysisWorker

        worker = MagicMock(spec=AnalysisWorker)
        worker.storage = MagicMock()
        analysis = {'id': 42, 'uuid': 'job-uuid', 'hints': json.dumps(hints)}
        process_cleanup(worker, analysis, {}, Path('/tmp'))
        return worker

    def test_deletes_keys_and_prefixes(self):
        worker = self._run({
            'artefact_keys': ['uploads/a.img'],
            'output_file_keys': ['outputs/v.png'],
            'output_dir_prefixes': ['outputs/item/art/'],
            'cache_prefixes': ['outputs/.cache/u'],
        })
        worker.storage.delete.assert_any_call('uploads/a.img')
        worker.storage.delete.assert_any_call('outputs/v.png')
        worker.storage.delete_prefix.assert_any_call('outputs/item/art/')
        worker.storage.delete_prefix.assert_any_call('outputs/.cache/u')
        worker.complete_analysis.assert_called_once()
        args, kwargs = worker.complete_analysis.call_args
        self.assertEqual(args[0], 42)
        self.assertIn('4 of 4', kwargs['summary'])
        worker.fail_analysis.assert_not_called()

    def test_missing_keys_are_tolerated(self):
        from worker.arcworker.analyses.cleanup import process_cleanup
        from worker.arcworker.analysis import AnalysisWorker

        worker = MagicMock(spec=AnalysisWorker)
        worker.storage = MagicMock()
        worker.storage.delete.side_effect = FileNotFoundError()
        analysis = {'id': 7, 'uuid': 'j', 'hints': json.dumps({
            'artefact_keys': ['uploads/gone.img'],
        })}
        process_cleanup(worker, analysis, {}, Path('/tmp'))
        worker.complete_analysis.assert_called_once()
        worker.fail_analysis.assert_not_called()

    def test_invalid_hints_fail_the_job(self):
        from worker.arcworker.analyses.cleanup import process_cleanup
        from worker.arcworker.analysis import AnalysisWorker

        worker = MagicMock(spec=AnalysisWorker)
        worker.storage = MagicMock()
        analysis = {'id': 9, 'uuid': 'j', 'hints': '{not json'}
        process_cleanup(worker, analysis, {}, Path('/tmp'))
        worker.fail_analysis.assert_called_once()
        worker.complete_analysis.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
