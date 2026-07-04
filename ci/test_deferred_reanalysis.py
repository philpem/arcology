"""
Deferred (reset-on-claim) re-analysis tests.

A re-analysis no longer clears the artefact up front; instead it queues a
single CLEANUP job carrying a REANALYSIS_RESET marker.  The previous run's
analyses, derived artefacts and partitions stay visible until a worker claims
that trigger job, at which point the API claim path performs the destructive
reset and queues the replacement analyses (in the same transaction), then the
job does its normal storage-output cleanup on the worker.

Covers:
  - queue_deferred_reanalysis(): queues the trigger and preserves the old
    results (nothing deleted yet).
  - apply_deferred_reanalysis_reset(): swaps old results for freshly queued
    replacement analyses, sparing the trigger, and strips the marker.
  - idempotency: a second apply (e.g. after a stale re-claim) is a no-op and
    never deletes the replacement analyses.
  - the CLEANUP dispatch barrier holds the replacement analyses back until the
    trigger job is terminal.
  - the worker-claim API route (PUT /api/analysis/<id>) applies the reset.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_deferred_reanalysis -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-deferred-reanalysis-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class _DeferredBase(unittest.TestCase):
    def setUp(self):
        from arcology_shared.storage import create_storage
        from myapp.app import create_app
        from myapp.extensions import db as _db

        self.app = create_app()
        self.app.config['TESTING'] = True

        self.tmpdir = tempfile.mkdtemp()
        self.app.config['UPLOAD_FOLDER'] = os.path.join(self.tmpdir, 'uploads')
        self.app.config['OUTPUT_FOLDER'] = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(self.app.config['UPLOAD_FOLDER'], exist_ok=True)
        os.makedirs(self.app.config['OUTPUT_FOLDER'], exist_ok=True)
        self.app.storage = create_storage(dict(self.app.config))

        self.db = _db
        with self.app.app_context():
            _db.create_all()

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.drop_all()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_tree(self):
        """Root HFE artefact with a completed analysis, a derived artefact and a
        partition — a stand-in for a finished analysis run.  Returns ids."""
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            Artefact,
            FilesystemType,
            Item,
            Partition,
            StorageDirectory,
        )

        item = Item(name='Deferred Item')
        self.db.session.add(item)
        self.db.session.flush()

        root = Artefact(
            item_id=item.id, label='Root', artefact_type=ArtefactType.HFE,
            original_filename='disk.hfe', storage_path='disk.hfe',
            storage_directory=StorageDirectory.UPLOADS,
        )
        self.db.session.add(root)
        self.db.session.flush()

        old = Analysis(
            artefact_id=root.id, analysis_type=AnalysisType.FLUX_DECODE,
            status=AnalysisStatus.COMPLETED,
            output_path=f'itempart/{root.uuid}_slug/flux_decode',
            details=json.dumps({'outputs': [{'filename': 'vis.png'}]}),
        )
        self.db.session.add(old)
        self.db.session.flush()

        derived = Artefact(
            item_id=item.id, label='Derived SCP', artefact_type=ArtefactType.SCP,
            original_filename='disk.scp', storage_path='disk.scp',
            storage_directory=StorageDirectory.OUTPUTS,
            parent_artefact_id=root.id, derived_from_analysis_id=old.id,
        )
        self.db.session.add(derived)
        self.db.session.add(Partition(artefact_id=root.id, partition_index=0,
                                      label='Main', filesystem=FilesystemType.DFS))
        self.db.session.commit()
        return {
            'item_id': item.id, 'root_id': root.id, 'old_id': old.id,
            'derived_id': derived.id,
        }

    def _cleanup_trigger(self, artefact_id):
        """The PENDING CLEANUP trigger job for an artefact, or None."""
        from arcology_shared.enums import AnalysisType
        from myapp.database import Analysis, AnalysisStatus
        return Analysis.query.filter_by(
            artefact_id=artefact_id, analysis_type=AnalysisType.CLEANUP,
            status=AnalysisStatus.PENDING,
        ).first()

    def _replacement_analyses(self, artefact_id, trigger_id):
        """Non-CLEANUP PENDING analyses queued on the root (the replacements)."""
        from arcology_shared.enums import AnalysisType
        from myapp.database import Analysis, AnalysisStatus
        return Analysis.query.filter(
            Analysis.artefact_id == artefact_id,
            Analysis.status == AnalysisStatus.PENDING,
            Analysis.analysis_type != AnalysisType.CLEANUP,
            Analysis.id != trigger_id,
        ).all()


class TestQueueDeferredReanalysis(_DeferredBase):
    def test_queues_trigger_and_preserves_results(self):
        from arcology_shared.enums import AnalysisType
        from arcology_shared.hints import HintKey
        from myapp.database import (
            ANALYSIS_PRIORITY_LOW,
            Analysis,
            AnalysisStatus,
            Artefact,
            Partition,
        )
        from myapp.services.artefact_lifecycle import (
            queue_deferred_reanalysis,
            reanalysis_reset_marker,
        )

        with self.app.app_context():
            ids = self._make_tree()
            root = self.db.session.get(Artefact, ids['root_id'])

            trigger = queue_deferred_reanalysis(
                root, hints={'platform': 'Acorn'}, priority=ANALYSIS_PRIORITY_LOW)

            # Trigger is a PENDING CLEANUP on the root, at the requested priority.
            self.assertEqual(trigger.analysis_type, AnalysisType.CLEANUP)
            self.assertEqual(trigger.status, AnalysisStatus.PENDING)
            self.assertEqual(trigger.artefact_id, ids['root_id'])
            self.assertEqual(trigger.priority, ANALYSIS_PRIORITY_LOW)

            # It carries both the marker (with the requeue params) and the
            # storage-cleanup payload the worker will consume.
            marker = reanalysis_reset_marker(trigger)
            self.assertIsNotNone(marker)
            self.assertEqual(marker['priority'], ANALYSIS_PRIORITY_LOW)
            self.assertEqual(marker['analysis_hints'], {'platform': 'Acorn'})
            hints = json.loads(trigger.hints)
            self.assertIn(HintKey.OUTPUT_DIR_PREFIXES, hints)
            self.assertIn(f'outputs/itempart/{root.uuid}_slug/flux_decode',
                          hints[HintKey.OUTPUT_DIR_PREFIXES])

            # Nothing destroyed yet: old analysis, derived artefact and partition
            # all still present.
            self.assertIsNotNone(self.db.session.get(Analysis, ids['old_id']))
            self.assertIsNotNone(self.db.session.get(Artefact, ids['derived_id']))
            self.assertEqual(
                Partition.query.filter_by(artefact_id=ids['root_id']).count(), 1)
            # No replacement analyses queued up front.
            self.assertEqual(self._replacement_analyses(ids['root_id'], trigger.id), [])


class TestApplyDeferredReset(_DeferredBase):
    def _claim_and_apply(self, ids):
        """Simulate a worker claim: mark the trigger RUNNING, apply the reset."""
        from myapp.database import Analysis, AnalysisStatus
        from myapp.services.artefact_lifecycle import apply_deferred_reanalysis_reset
        trigger = self._cleanup_trigger(ids['root_id'])
        trigger.status = AnalysisStatus.RUNNING
        apply_deferred_reanalysis_reset(trigger)
        self.db.session.commit()
        return self.db.session.get(Analysis, trigger.id)

    def test_swaps_old_results_for_replacements(self):
        from arcology_shared.enums import AnalysisType
        from myapp.database import (
            ANALYSIS_PRIORITY_LOW,
            Analysis,
            Artefact,
            Partition,
        )
        from myapp.services.artefact_lifecycle import (
            queue_deferred_reanalysis,
            reanalysis_reset_marker,
        )

        with self.app.app_context():
            ids = self._make_tree()
            root = self.db.session.get(Artefact, ids['root_id'])
            queue_deferred_reanalysis(root, priority=ANALYSIS_PRIORITY_LOW)

            trigger = self._claim_and_apply(ids)

            # Old results are gone.
            self.assertIsNone(self.db.session.get(Analysis, ids['old_id']))
            self.assertIsNone(self.db.session.get(Artefact, ids['derived_id']))
            self.assertEqual(
                Partition.query.filter_by(artefact_id=ids['root_id']).count(), 0)

            # Trigger survived the reset it drove, and its marker was stripped so
            # the leftover is a plain storage-cleanup job.
            self.assertIsNotNone(trigger)
            self.assertIsNone(reanalysis_reset_marker(trigger))

            # Replacement analyses were queued at the requested priority and
            # always include the checksum job.
            replacements = self._replacement_analyses(ids['root_id'], trigger.id)
            self.assertTrue(replacements)
            self.assertIn(AnalysisType.CHECKSUM_COMPUTE,
                          {a.analysis_type for a in replacements})
            self.assertTrue(all(a.priority == ANALYSIS_PRIORITY_LOW for a in replacements))

    def test_reset_is_idempotent(self):
        """A second apply (after a stale re-claim) must not re-run the reset and
        delete the freshly queued replacement analyses."""
        from myapp.database import ANALYSIS_PRIORITY_LOW, Artefact
        from myapp.services.artefact_lifecycle import (
            apply_deferred_reanalysis_reset,
            queue_deferred_reanalysis,
            reanalysis_reset_marker,
        )

        with self.app.app_context():
            ids = self._make_tree()
            root = self.db.session.get(Artefact, ids['root_id'])
            queue_deferred_reanalysis(root, priority=ANALYSIS_PRIORITY_LOW)

            trigger = self._claim_and_apply(ids)
            before = {a.id for a in self._replacement_analyses(ids['root_id'], trigger.id)}
            self.assertTrue(before)

            # Marker already stripped -> apply is a no-op.
            self.assertIsNone(reanalysis_reset_marker(trigger))
            apply_deferred_reanalysis_reset(trigger)
            self.db.session.commit()

            after = {a.id for a in self._replacement_analyses(ids['root_id'], trigger.id)}
            self.assertEqual(before, after)


class TestBarrierHoldsReplacements(_DeferredBase):
    def test_replacements_gated_on_trigger_terminal(self):
        from myapp.database import (
            ANALYSIS_PRIORITY_LOW,
            Analysis,
            AnalysisStatus,
            Artefact,
        )
        from myapp.services.analysis_queue import pending_claimable_query
        from myapp.services.artefact_lifecycle import (
            apply_deferred_reanalysis_reset,
            queue_deferred_reanalysis,
        )

        with self.app.app_context():
            ids = self._make_tree()
            root = self.db.session.get(Artefact, ids['root_id'])
            queue_deferred_reanalysis(root, priority=ANALYSIS_PRIORITY_LOW)

            trigger = self._cleanup_trigger(ids['root_id'])
            trigger.status = AnalysisStatus.RUNNING
            apply_deferred_reanalysis_reset(trigger)
            self.db.session.commit()

            def claimable_ids():
                return {a.id for a in pending_claimable_query(apply_heavy_cap=False).all()}

            replacements = {
                a.id for a in self._replacement_analyses(ids['root_id'], trigger.id)}
            self.assertTrue(replacements)

            # Trigger RUNNING -> the barrier withholds the artefact's replacements.
            claimable = claimable_ids()
            self.assertFalse(replacements & claimable)

            # Trigger terminal -> the barrier lifts and they become claimable.
            trigger = self.db.session.get(Analysis, trigger.id)
            trigger.status = AnalysisStatus.COMPLETED
            self.db.session.commit()
            self.assertTrue(replacements <= claimable_ids())


class TestClaimRouteAppliesReset(_DeferredBase):
    def test_worker_claim_triggers_reset(self):
        from arcology_shared.enums import AnalysisType
        from myapp.database import ANALYSIS_PRIORITY_LOW, Analysis, Artefact
        from myapp.services.artefact_lifecycle import (
            queue_deferred_reanalysis,
            reanalysis_reset_marker,
        )

        with self.app.app_context():
            ids = self._make_tree()
            root = self.db.session.get(Artefact, ids['root_id'])
            trigger = queue_deferred_reanalysis(root, priority=ANALYSIS_PRIORITY_LOW)
            trigger_id = trigger.id

        client = self.app.test_client()
        resp = client.put(
            f'/api/analysis/{trigger_id}',
            headers={'X-API-Key': _WORKER_KEY},
            json={'claim_worker': True, 'status': 'running'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['claimed'])

        with self.app.app_context():
            # Old results cleared, replacements queued, marker stripped.
            self.assertIsNone(self.db.session.get(Analysis, ids['old_id']))
            self.assertIsNone(self.db.session.get(Artefact, ids['derived_id']))
            trigger = self.db.session.get(Analysis, trigger_id)
            self.assertIsNone(reanalysis_reset_marker(trigger))
            replacements = self._replacement_analyses(ids['root_id'], trigger_id)
            self.assertTrue(replacements)
            self.assertIn(AnalysisType.CHECKSUM_COMPUTE,
                          {a.analysis_type for a in replacements})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
