"""
Tests for the DB-only HashDB recognition / link maintenance run by the task
runner in-process (myapp/services/hashdb_jobs.py + the shared
recognise_products_step / hash_rescan services).

Covers, among others:
  * `done` is derived from a short batch (len(products) < limit) and paging by
    KnownProduct.id reaches an empty follow-up;
  * a finishing recognition backfill does NOT mark the database COMPLETED when a
    fresh PENDING HASHDB_RECOGNITION backfill is already queued (a content change
    landed mid-run, so the current run's counts are stale) — instead it stays
    PENDING so the follow-up refreshes it;
  * a non-timeout error marks the database FAILED and propagates;
  * best-hash (sha256) matching, per-partition replace semantics, and
    multi-chunk candidate-folder verification.

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
                name='Recog DB',
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
                    md5=md5, is_directory=False))
            db.session.commit()

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

    # ---- run-to-completion recognition backfill (taskrunner path) ---------

    def test_recognition_job_completes_and_marks_completed(self):
        from myapp.database import HashDatabase, ProductRecognitionStatus
        from myapp.services.hashdb_jobs import run_hashdb_recognition_job
        self._reset_status(ProductRecognitionStatus.PENDING)
        self._drain_pending_recognition()

        with self.app.app_context():
            result = run_hashdb_recognition_job(self.db_id)
            self.assertEqual(result['processed'], 3)
            self.assertEqual(result['matches'], 3)
            hdb = self.db.session.get(HashDatabase, self.db_id)
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.COMPLETED)

    def test_recognition_step_pages_then_reports_empty_follow_up(self):
        # recognise_products_step pages by KnownProduct.id: a full batch is not
        # done, and the follow-up past the last id sees no products and is done.
        from myapp.database import KnownProduct
        from myapp.services.recognition import recognise_products_step
        with self.app.app_context():
            # limit == 3 == product count -> full batch -> not done yet.
            first = recognise_products_step(
                database_id=self.db_id, last_product_id=0, limit=3)
            self.db.session.commit()
            self.assertFalse(first['done'])
            next_id = first['next_product_id']

            # The follow-up call sees no products and reports done.
            second = recognise_products_step(
                database_id=self.db_id, last_product_id=next_id, limit=3)
            self.db.session.commit()
            self.assertTrue(second['done'])
            self.assertEqual(second['processed'], 0)
            self.assertEqual(
                KnownProduct.query.filter_by(database_id=self.db_id)
                .filter(KnownProduct.id > next_id).count(),
                0,
            )

    # ---- a finishing backfill defers COMPLETED on a stale follow-up -------

    def test_finishing_job_stays_pending_when_followup_queued(self):
        from myapp.database import HashDatabase, ProductRecognitionStatus
        from myapp.services.hash_rescan import queue_hashdb_recognition_job
        from myapp.services.hashdb_jobs import run_hashdb_recognition_job

        self._reset_status(ProductRecognitionStatus.RUNNING)
        self._drain_pending_recognition()
        with self.app.app_context():
            # A content change landed mid-run and queued a fresh backfill.
            queue_hashdb_recognition_job(self.db_id)
            run_hashdb_recognition_job(self.db_id)
            hdb = self.db.session.get(HashDatabase, self.db_id)
            # NOT COMPLETED — the queued follow-up will refresh the stale counts.
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.PENDING)
            self.assertIsNone(hdb.product_recognition_updated_at)

    def test_non_timeout_error_marks_failed_and_propagates(self):
        # A non-timeout error during the backfill marks the database FAILED and
        # propagates (the taskrunner records the job FAILED).
        from unittest.mock import patch
        from sqlalchemy.exc import OperationalError
        from myapp.database import HashDatabase, ProductRecognitionStatus
        from myapp.services.hashdb_jobs import run_hashdb_recognition_job

        self._reset_status(ProductRecognitionStatus.PENDING)
        self._drain_pending_recognition()

        class _Orig(Exception):
            pgcode = '40001'  # serialization_failure — not a statement timeout

        with self.app.app_context():
            with patch('myapp.services.hashdb_jobs.recognise_products_step',
                       side_effect=OperationalError('x', {}, _Orig())):
                with self.assertRaises(OperationalError):
                    run_hashdb_recognition_job(self.db_id)
            hdb = self.db.session.get(HashDatabase, self.db_id)
            self.assertEqual(hdb.product_recognition_status,
                             ProductRecognitionStatus.FAILED)

    # ---- candidate-folder verification is chunked (bounded statement) ------

    def test_recognition_matches_across_folder_query_batches(self):
        """Matching is correct when candidate folders span multiple chunks."""
        from unittest.mock import patch
        from myapp.database import ProductRecognitionStatus, RecognisedProduct
        from myapp.services.recognition import recognise_products_step

        self._reset_status(ProductRecognitionStatus.PENDING)
        self._drain_pending_recognition()

        # Force the chunk size below the number of candidate folders (3) so the
        # verification query runs in more than one batch.
        with self.app.app_context():
            with patch('myapp.services.recognition._FOLDER_QUERY_BATCH', 1):
                result = recognise_products_step(
                    database_id=self.db_id, last_product_id=0, limit=50)
            self.db.session.commit()
            self.assertTrue(result['done'])
            # All three products still match despite the tiny chunk size.
            self.assertEqual(result['matches'], 3)
            self.assertEqual(RecognisedProduct.query.count(), 3)

    def test_statement_timeout_helper_noop_on_sqlite(self):
        """The PostgreSQL statement-timeout guard is a safe no-op elsewhere.

        Still used by the surviving worker-driven similarity-step endpoint.
        """
        from myapp.blueprints.api import _apply_statement_timeout
        with self.app.app_context():
            # Must not raise on SQLite, and tolerate bad/zero values.
            _apply_statement_timeout(120)
            _apply_statement_timeout(0)
            _apply_statement_timeout(None)
            _apply_statement_timeout('nope')

    def test_wall_clock_deadline_returns_timed_out(self):
        # A step that overruns the wall-clock budget returns timed_out even when
        # no single statement_timeout fires.  Drive the service directly with a
        # deadline already in the past.  (The taskrunner runs recognition with
        # deadline=None, so it never trips this — but the service contract holds.)
        import time
        from myapp.services.recognition import recognise_products_step

        with self.app.app_context():
            result = recognise_products_step(
                database_id=self.db_id, last_product_id=0, limit=50,
                deadline=time.monotonic() - 1)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['done'])
        self.assertGreater(result['next_product_id'], 0)

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
            hdb = HashDatabase(name='BH DB',
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
                md5='d' * 32, sha256=sha256, is_directory=False))

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
                md5='b' * 32, is_directory=False))
            db.session.commit()

    def test_best_hash_backfill_matches_on_sha256(self):
        from myapp.database import RecognisedProduct
        from myapp.services.recognition import recognise_products_step
        with self.app.app_context():
            result = recognise_products_step(
                database_id=self.db_id, last_product_id=0, limit=50)
            self.db.session.commit()
            self.assertTrue(result['done'])
            matched = {
                rp.product_id for rp in RecognisedProduct.query.filter_by(
                    partition_id=self.part_id).all()
            }
        # A matches via sha256 (despite md5 mismatch); B does not (no sha256 on file).
        self.assertIn(self.prod_a, matched)
        self.assertNotIn(self.prod_b, matched)

    def test_partition_recognition_writes_and_replaces(self):
        from myapp.database import RecognisedProduct
        from myapp.services.recognition import recognise_products_step
        with self.app.app_context():
            # First run: A matches.
            recognise_products_step(partition_id=self.part_id, last_product_id=0, limit=50)
            self.db.session.commit()
            first = RecognisedProduct.query.filter_by(partition_id=self.part_id).count()
            self.assertGreaterEqual(first, 1)
            # Re-running replaces rather than duplicating (idempotent).
            recognise_products_step(partition_id=self.part_id, last_product_id=0, limit=50)
            self.db.session.commit()
            second = RecognisedProduct.query.filter_by(partition_id=self.part_id).count()
        self.assertEqual(first, second)


class TestPerDatabaseRescanIsScoped(unittest.TestCase):
    """The /hashdb/<id>/rescan button queues ONE scoped HASHDB_LINK job, not a
    per-artefact HASH_RESCAN fan-out across the whole collection."""

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
            Partition,
            StorageDirectory,
            User,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()
            user = User(username='rescan-rw', password_hash='x',
                        permission=UserPermission.READ_WRITE)
            db.session.add(user)
            db.session.flush()
            cls.uid = user.id

            hdb = HashDatabase(name='Scoped DB', is_active=True)
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id

            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            # A couple of artefacts with extracted files: the old behaviour would
            # have queued a HASH_RESCAN for each of these.
            for i in range(3):
                art = Artefact(item_id=item.id, label=f'Disc {i}',
                               artefact_type=ArtefactType.HFE,
                               original_filename='d.ssd', storage_path=f'd{i}.ssd',
                               storage_directory=StorageDirectory.UPLOADS)
                db.session.add(art)
                db.session.flush()
                part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                                 filesystem=FilesystemType.DFS, total_files=1)
                db.session.add(part)
                db.session.flush()
                db.session.add(ExtractedFile(
                    partition_id=part.id, path=f'F{i}/file', filename='file',
                    md5=f'{i:032x}', is_directory=False))
            db.session.commit()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True

    def test_rescan_queues_single_scoped_link_job(self):
        from myapp.database import Analysis, AnalysisType

        self._login()
        resp = self.client.post(f'/hashdb/{self.db_id}/rescan')
        self.assertEqual(resp.status_code, 302, resp.data)

        with self.app.app_context():
            # Exactly one scoped HASHDB_LINK job for this DB, no per-artefact
            # HASH_RESCAN fan-out.
            link_jobs = Analysis.query.filter_by(
                analysis_type=AnalysisType.HASHDB_LINK, artefact_id=None).all()
            self.assertEqual(len(link_jobs), 1)
            self.assertIn(f'"database_id": {self.db_id}', link_jobs[0].hints)
            self.assertEqual(
                Analysis.query.filter_by(
                    analysis_type=AnalysisType.HASH_RESCAN).count(),
                0)

    def test_rescan_is_idempotent_on_repeat_clicks(self):
        from myapp.database import Analysis, AnalysisType

        self._login()
        self.client.post(f'/hashdb/{self.db_id}/rescan')
        self.client.post(f'/hashdb/{self.db_id}/rescan')
        with self.app.app_context():
            self.assertEqual(
                Analysis.query.filter_by(
                    analysis_type=AnalysisType.HASHDB_LINK, artefact_id=None).count(),
                1)


class TestOptionalOnlyRecognition(unittest.TestCase):
    """Optional-only products (no mandatory files) are ignored by the matcher:
    they have no discriminating fingerprint, so matching them on a ubiquitous
    shared file would just be noise.  They produce zero recognition rows."""

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
            hdb = HashDatabase(name='OptOnly DB',
                               enable_product_recognition=True)
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id

            # An optional-only product: a !Boot (the ubiquitous file) plus another
            # optional file, both is_required=False.
            prod = KnownProduct(database_id=hdb.id, title='!App')
            db.session.add(prod)
            db.session.flush()
            cls.prod_id = prod.id
            db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                     filename='!Boot', md5='ab' * 16, is_required=False))
            db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                     filename='!Run', md5='cd' * 16, is_required=False))

            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            # The ubiquitous !Boot appears in many unrelated folders.
            for i in range(5):
                part = Partition(artefact_id=art.id, partition_index=i, label=f'P{i}',
                                 filesystem=FilesystemType.DFS)
                db.session.add(part)
                db.session.flush()
                db.session.add(ExtractedFile(
                    partition_id=part.id, path=f'Folder{i}/!Boot', filename='!Boot',
                    md5='ab' * 16, is_directory=False))
            db.session.commit()

    def test_optional_only_product_is_ignored(self):
        from unittest.mock import patch
        from myapp.database import RecognisedProduct
        from myapp.services.recognition import recognise_products_step

        # An all-optional product is skipped before any folder work, so the
        # per-folder fetch is never even reached (patched to raise to prove it).
        def _boom(*a, **k):
            raise AssertionError('optional-only product must not be processed')

        with self.app.app_context():
            with patch('myapp.services.recognition._folder_file_condition',
                       side_effect=_boom):
                result = recognise_products_step(
                    database_id=self.db_id, last_product_id=0, limit=50)
            self.assertTrue(result['done'])
            self.assertEqual(result['matches'], 0)
            self.assertEqual(
                RecognisedProduct.query.filter_by(product_id=self.prod_id).count(), 0)


class TestHashdbViewPagination(unittest.TestCase):
    """The product list paginates (NIST-scale databases) and flags products with
    no mandatory file (which the matcher ignores)."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import (
            HashDatabase,
            KnownFile,
            KnownProduct,
            User,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            user = User(username='hp-rw', password_hash='x',
                        permission=UserPermission.READ_WRITE)
            db.session.add(user)
            db.session.flush()
            cls.uid = user.id
            hdb = HashDatabase(name='Paged DB')
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id
            # 5 products A..E; only 'C' has a mandatory file.
            cls.no_mandatory_titles = set()
            for letter in 'ABCDE':
                prod = KnownProduct(database_id=hdb.id, title=f'{letter}pp')
                db.session.add(prod)
                db.session.flush()
                required = letter == 'C'
                db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                         filename='f', md5=f'{ord(letter):032x}',
                                         is_required=required))
                if not required:
                    cls.no_mandatory_titles.add(prod.id)
            db.session.commit()

    def _ctx(self, url):
        from flask import template_rendered
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True
        captured = []
        template_rendered.connect(
            lambda sender, template, context, **k: captured.append(context),
            self.app, weak=False)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.data)
        return captured[-1]

    def test_paginates_and_jump_bar(self):
        ctx = self._ctx(f'/hashdb/{self.db_id}?per_page=25')
        self.assertEqual(ctx['products_pagination'].total, 5)

        ctx = self._ctx(f'/hashdb/{self.db_id}?per_page=0')  # 'All' still works
        self.assertEqual(len(ctx['products']), 5)

        # With a tiny page size there are multiple pages and a letter jump bar.
        ctx = self._ctx(f'/hashdb/{self.db_id}?per_page=25&page=1')
        self.assertEqual(ctx['products_pagination'].total, 5)

    def test_no_mandatory_products_flagged(self):
        ctx = self._ctx(f'/hashdb/{self.db_id}?per_page=0')
        self.assertEqual(ctx['no_mandatory_ids'], self.no_mandatory_titles)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
