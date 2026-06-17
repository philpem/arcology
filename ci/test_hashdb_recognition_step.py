"""
Tests for the worker-driven bounded HashDB maintenance steps:
  POST /api/hash-databases/<id>/link-step
  POST /api/hash-databases/<id>/recognition-step

Covers two specific behaviours:
  * `done` is derived from a short batch (len(batch) < limit) rather than an
    extra COUNT over the remaining tail, and no `remaining` field is returned.
  * A finishing recognition step does NOT mark the database COMPLETED when a
    fresh PENDING HASHDB_RECOGNITION backfill is already queued (a content
    change landed mid-run, so the current run's counts are stale).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_recognition_step -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-recognition-step-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestHashdbRecognitionStep(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
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
            ProductRecognitionStatus,
            StorageDirectory,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            hdb = HashDatabase(
                name='Recog DB', file_count=0,
                enable_product_recognition=True,
                product_recognition_status=ProductRecognitionStatus.PENDING,
            )
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id

            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()

            # Three products, each with one required file that is present in its
            # own folder, so a full recognition pass produces three matches.
            for i in range(3):
                md5 = f'{i:032x}'
                prod = KnownProduct(database_id=hdb.id, title=f'!App{i}')
                db.session.add(prod)
                db.session.flush()
                db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                         filename='!Run', md5=md5, is_required=True))
                db.session.add(ExtractedFile(
                    partition_id=part.id, path=f'App{i}/!Run', filename='!Run',
                    md5=md5, is_directory=False, is_known=False))
            db.session.commit()

    def _post(self, path, payload):
        return self.client.post(f'/api/hash-databases/{self.db_id}{path}',
                                json=payload, headers={'X-API-Key': _WORKER_KEY})

    def _reset_status(self, status):
        from myapp.database import HashDatabase
        with self.app.app_context():
            hdb = self.db.session.get(HashDatabase, self.db_id)
            hdb.product_recognition_status = status
            hdb.product_recognition_updated_at = None
            self.db.session.commit()

    def _drain_pending_recognition(self):
        from myapp.database import Analysis, AnalysisStatus, AnalysisType
        with self.app.app_context():
            (Analysis.query
             .filter_by(artefact_id=None, analysis_type=AnalysisType.HASHDB_RECOGNITION,
                        status=AnalysisStatus.PENDING)
             .delete(synchronize_session=False))
            self.db.session.commit()

    # ---- fix 3: done via short batch, no `remaining` field ----------------

    def test_recognition_step_done_without_remaining_field(self):
        from myapp.database import ProductRecognitionStatus
        self._reset_status(ProductRecognitionStatus.PENDING)
        self._drain_pending_recognition()

        # limit > product count -> short batch -> done in one step.
        resp = self._post('/recognition-step', {'last_product_id': 0, 'limit': 50})
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertTrue(body['done'])
        self.assertEqual(body['processed'], 3)
        self.assertEqual(body['matches'], 3)
        self.assertNotIn('remaining', body)

        with self.app.app_context():
            from myapp.database import HashDatabase
            hdb = self.db.session.get(HashDatabase, self.db_id)
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.COMPLETED)

    def test_recognition_step_full_batch_then_empty_follow_up(self):
        from myapp.database import KnownProduct, ProductRecognitionStatus
        self._reset_status(ProductRecognitionStatus.PENDING)
        self._drain_pending_recognition()

        # limit == 3 == product count -> full batch -> not done yet.
        resp = self._post('/recognition-step', {'last_product_id': 0, 'limit': 3})
        body = resp.get_json()
        self.assertFalse(body['done'])
        next_id = body['next_product_id']

        # The follow-up call sees no products and reports done.
        resp = self._post('/recognition-step', {'last_product_id': next_id, 'limit': 3})
        body = resp.get_json()
        self.assertTrue(body['done'])
        self.assertEqual(body['processed'], 0)

        with self.app.app_context():
            self.assertEqual(
                KnownProduct.query.filter_by(database_id=self.db_id)
                .filter(KnownProduct.id > next_id).count(),
                0,
            )

    def test_link_step_done_without_remaining_field(self):
        resp = self._post('/link-step', {'last_id': 0, 'limit': 500})
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertTrue(body['done'])
        self.assertNotIn('remaining', body)

    # ---- fix 2: finishing step defers COMPLETED on a stale follow-up ------

    def test_finishing_step_stays_pending_when_followup_queued(self):
        from myapp.database import HashDatabase, ProductRecognitionStatus
        from myapp.services.hash_rescan import queue_hashdb_recognition_job

        self._reset_status(ProductRecognitionStatus.RUNNING)
        self._drain_pending_recognition()
        with self.app.app_context():
            # A content change landed mid-run and queued a fresh backfill.
            queue_hashdb_recognition_job(self.db_id)

        resp = self._post('/recognition-step', {'last_product_id': 0, 'limit': 50})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.get_json()['done'])

        with self.app.app_context():
            hdb = self.db.session.get(HashDatabase, self.db_id)
            # NOT COMPLETED — the queued follow-up will refresh the stale counts.
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.PENDING)
            self.assertIsNone(hdb.product_recognition_updated_at)

    def test_backfill_queue_commits_pending_status_when_job_already_pending(self):
        from myapp.database import HashDatabase, ProductRecognitionStatus
        from myapp.services.hash_rescan import (
            queue_hashdb_recognition_backfill,
            queue_hashdb_recognition_job,
        )

        self._reset_status(ProductRecognitionStatus.COMPLETED)
        self._drain_pending_recognition()
        with self.app.app_context():
            queue_hashdb_recognition_job(self.db_id)
            hdb = self.db.session.get(HashDatabase, self.db_id)
            hdb.product_recognition_status = ProductRecognitionStatus.COMPLETED
            self.db.session.commit()

            _, queued = queue_hashdb_recognition_backfill(hdb)

        self.assertFalse(queued)
        with self.app.app_context():
            hdb = self.db.session.get(HashDatabase, self.db_id)
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.PENDING)
            self.assertIsNone(hdb.product_recognition_updated_at)


class TestRecognitionBestHashAndPartition(unittest.TestCase):
    """DB-level recognition: best-hash (issue #620) and the per-partition step."""

    @classmethod
    def setUpClass(cls):
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

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()
            hdb = HashDatabase(name='BH DB', file_count=0,
                               enable_product_recognition=True)
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id

            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            cls.part_uuid = part.uuid
            cls.part_id = part.id

            sha256 = 'c' * 64
            # Product A: required file identified by SHA-256.  The extracted file
            # shares the sha256 but has a DIFFERENT md5 -> must still match (#620).
            prod_a = KnownProduct(database_id=hdb.id, title='!HashA')
            db.session.add(prod_a)
            db.session.flush()
            cls.prod_a = prod_a.id
            db.session.add(KnownFile(database_id=hdb.id, product_id=prod_a.id,
                                     filename='!Run', md5='a' * 32, sha256=sha256,
                                     is_required=True))
            db.session.add(ExtractedFile(
                partition_id=part.id, path='FolderA/!Run', filename='!Run',
                md5='d' * 32, sha256=sha256, is_directory=False, is_known=False))

            # Product B: required file's best hash is SHA-256, but the extracted
            # file in its folder has only md5 (no sha256) -> must NOT match.
            prod_b = KnownProduct(database_id=hdb.id, title='!HashB')
            db.session.add(prod_b)
            db.session.flush()
            cls.prod_b = prod_b.id
            db.session.add(KnownFile(database_id=hdb.id, product_id=prod_b.id,
                                     filename='!Run', md5='b' * 32, sha256='e' * 64,
                                     is_required=True))
            db.session.add(ExtractedFile(
                partition_id=part.id, path='FolderB/!Run', filename='!Run',
                md5='b' * 32, is_directory=False, is_known=False))
            db.session.commit()

    def test_best_hash_backfill_matches_on_sha256(self):
        from myapp.database import RecognisedProduct
        resp = self.client.post(
            f'/api/hash-databases/{self.db_id}/recognition-step',
            json={'last_product_id': 0, 'limit': 50},
            headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.get_json()['done'])
        with self.app.app_context():
            matched = {
                rp.product_id for rp in RecognisedProduct.query.filter_by(
                    partition_id=self.part_id).all()
            }
        # A matches via sha256 (despite md5 mismatch); B does not (no sha256 on file).
        self.assertIn(self.prod_a, matched)
        self.assertNotIn(self.prod_b, matched)

    def test_partition_step_writes_and_replaces(self):
        from myapp.database import RecognisedProduct
        path = f'/api/partitions/{self.part_uuid}/recognise-step'
        # First run: A matches.
        resp = self.client.post(path, json={'last_product_id': 0, 'limit': 50},
                                headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            first = RecognisedProduct.query.filter_by(partition_id=self.part_id).count()
        self.assertGreaterEqual(first, 1)
        # Re-running replaces rather than duplicating (idempotent).
        resp = self.client.post(path, json={'last_product_id': 0, 'limit': 50},
                                headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            second = RecognisedProduct.query.filter_by(partition_id=self.part_id).count()
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
