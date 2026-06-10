"""
Tests for the shared upload ingest pipeline (myapp/services/upload_pipeline.py).

Verifies that:
  - ingest_uploaded_artefact() creates the artefact, its slug, and its queued
    analyses atomically, honouring the three queue modes.
  - A duplicate upload (same item + SHA-256) returns the existing artefact and
    deletes the newly stored file.
  - A failure mid-transaction rolls back the artefact row AND deletes the
    stored file (no orphans in DB or storage).
  - The API upload endpoints (single and chunked) drive the same pipeline:
    201 with slug + queued_analyses, 409 on duplicate, and no artefact row is
    left behind when the request is rejected (invalid hints).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_upload_pipeline -v
"""

import io
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-upload-pipeline-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_AUTH = {'X-API-Key': os.environ['WORKER_API_KEY']}


class FakeStorage:
    """Minimal in-memory storage backend for upload tests."""

    def __init__(self):
        self.files = {}
        self.deleted = []

    def storage_key(self, directory, name):
        return f'{directory}/{name}'

    def put(self, key, local_path):
        with open(local_path, 'rb') as f:
            self.files[key] = f.read()

    def open_read(self, key):
        return io.BytesIO(self.files[key])

    def delete(self, key):
        self.deleted.append(key)
        self.files.pop(key, None)


class UploadPipelineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()
            from myapp.database import Item
            item = Item(name='Pipeline Test Item')
            _db.session.add(item)
            _db.session.commit()
            cls.item_id = item.id
            cls.item_uuid = item.uuid

    def setUp(self):
        self.storage = FakeStorage()
        self.app.storage = self.storage
        with self.app.app_context():
            from myapp.database import Analysis, Artefact
            Analysis.query.delete()
            Artefact.query.delete()
            self.db.session.commit()

    def _item(self):
        from myapp.database import Item
        return self.db.session.get(Item, self.item_id)

    def _ingest(self, **overrides):
        from myapp.services.upload_pipeline import ingest_uploaded_artefact
        from shared.enums import ArtefactType
        kwargs = dict(
            label='Test Disc',
            artefact_type=ArtefactType.RAW_SECTOR,
            type_overridden=False,
            original_filename='test.adf',
            storage_name='abc123.adf',
            file_size=1024,
            md5='d' * 32,
            sha256='e' * 64,
        )
        kwargs.update(overrides)
        return ingest_uploaded_artefact(self._item(), **kwargs)


class TestIngestService(UploadPipelineTestBase):
    """Direct tests of ingest_uploaded_artefact()."""

    def test_creates_artefact_with_slug_and_analyses(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        from myapp.database import Analysis, Artefact
        from shared.enums import AnalysisType

        with self.app.app_context():
            outcome = self._ingest()
            self.assertIsNone(outcome.duplicate)
            artefact = outcome.artefact
            self.assertIsNotNone(artefact.id)
            self.assertEqual(artefact.slug, 'test-disc')

            expected = [AnalysisType.CHECKSUM_COMPUTE] + ANALYSIS_MAP[artefact.artefact_type]
            self.assertEqual(outcome.queued_analyses, expected)
            db_types = {a.analysis_type for a in Analysis.query.filter_by(artefact_id=artefact.id)}
            self.assertEqual(db_types, set(expected))
            self.assertEqual(Artefact.query.count(), 1)

    def test_checksum_only_mode(self):
        from myapp.services.upload_pipeline import QUEUE_CHECKSUM_ONLY
        from shared.enums import AnalysisType

        with self.app.app_context():
            outcome = self._ingest(queue=QUEUE_CHECKSUM_ONLY)
            self.assertEqual(outcome.queued_analyses, [AnalysisType.CHECKSUM_COMPUTE])

    def test_queue_none_mode(self):
        from myapp.database import Analysis
        from myapp.services.upload_pipeline import QUEUE_NONE

        with self.app.app_context():
            outcome = self._ingest(queue=QUEUE_NONE)
            self.assertEqual(outcome.queued_analyses, [])
            self.assertEqual(Analysis.query.count(), 0)

    def test_duplicate_returns_existing_and_deletes_file(self):
        from myapp.database import Artefact

        with self.app.app_context():
            first = self._ingest().artefact
            outcome = self._ingest(storage_name='second-copy.adf')
            self.assertIsNone(outcome.artefact)
            self.assertEqual(outcome.duplicate.id, first.id)
            self.assertEqual(Artefact.query.count(), 1)
            self.assertIn('uploads/second-copy.adf', self.storage.deleted)

    def test_failure_rolls_back_artefact_and_deletes_file(self):
        from myapp.database import Analysis, Artefact

        with self.app.app_context():
            with mock.patch('myapp.services.upload_pipeline.ensure_unique_slug',
                            side_effect=RuntimeError('boom')):
                with self.assertRaises(RuntimeError):
                    self._ingest()
            self.assertEqual(Artefact.query.count(), 0)
            self.assertEqual(Analysis.query.count(), 0)
            self.assertIn('uploads/abc123.adf', self.storage.deleted)

    def test_integrity_error_without_winner_reraises_and_cleans_up(self):
        from sqlalchemy.exc import IntegrityError
        from myapp.database import Artefact

        with self.app.app_context():
            with mock.patch('myapp.services.upload_pipeline.ensure_unique_slug',
                            side_effect=IntegrityError('stmt', {}, Exception('dup'))):
                with self.assertRaises(IntegrityError):
                    self._ingest()
            self.assertEqual(Artefact.query.count(), 0)
            self.assertIn('uploads/abc123.adf', self.storage.deleted)


class TestApiUploadEndpoint(UploadPipelineTestBase):
    """The API single-upload endpoint drives the shared pipeline."""

    def _post_upload(self, content=b'API upload content', **form_overrides):
        data = {
            'file': (io.BytesIO(content), 'apitest.adf'),
            'label': 'API Disc',
        }
        data.update(form_overrides)
        return self.client.post(
            f'/api/items/{self.item_uuid}/artefacts/upload',
            data=data, content_type='multipart/form-data', headers=_AUTH,
        )

    def test_upload_creates_artefact(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        from myapp.database import Analysis, Artefact
        from shared.enums import AnalysisType

        resp = self._post_upload()
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.get_json()
        self.assertEqual(data['slug'], 'api-disc')
        with self.app.app_context():
            artefact = Artefact.query.one()
            expected_map = ANALYSIS_MAP[artefact.artefact_type]
            self.assertEqual(data['queued_analyses'], [t.value for t in expected_map])
            db_types = {a.analysis_type for a in Analysis.query.all()}
            self.assertEqual(db_types, {AnalysisType.CHECKSUM_COMPUTE} | set(expected_map))

    def test_duplicate_upload_returns_409(self):
        from myapp.database import Artefact

        resp1 = self._post_upload()
        self.assertEqual(resp1.status_code, 201, resp1.data)
        resp2 = self._post_upload()
        self.assertEqual(resp2.status_code, 409, resp2.data)
        self.assertTrue(resp2.get_json()['duplicate'])
        self.assertEqual(resp2.get_json()['uuid'], resp1.get_json()['uuid'])
        with self.app.app_context():
            self.assertEqual(Artefact.query.count(), 1)
        # The duplicate's stored file must have been cleaned up
        self.assertEqual(len(self.storage.deleted), 1)

    def test_auto_analyse_false_queues_nothing(self):
        from myapp.database import Analysis

        resp = self._post_upload(auto_analyse='false')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.get_json()['queued_analyses'], [])
        with self.app.app_context():
            self.assertEqual(Analysis.query.count(), 0)

    def test_invalid_hints_creates_no_artefact(self):
        """A 400 response must not leave a committed artefact behind."""
        from myapp.database import Artefact

        resp = self._post_upload(hints='not-json')
        self.assertEqual(resp.status_code, 400, resp.data)
        with self.app.app_context():
            self.assertEqual(Artefact.query.count(), 0)


class TestChunkedUploadEndpoint(UploadPipelineTestBase):
    """The chunked-upload completion endpoint drives the shared pipeline."""

    def test_chunked_upload_round_trip(self):
        from myapp.database import Artefact

        init = self.client.post('/api/uploads/chunked/init', json={
            'filename': 'chunked.adf',
            'total_chunks': 2,
            'item_uuid': self.item_uuid,
            'label': 'Chunked Disc',
        }, headers=_AUTH)
        self.assertEqual(init.status_code, 201, init.data)
        upload_uuid = init.get_json()['upload_uuid']

        for idx, payload in enumerate((b'first-half;', b'second-half')):
            resp = self.client.post(
                f'/api/uploads/chunked/{upload_uuid}/chunk/{idx}',
                data=payload, content_type='application/octet-stream', headers=_AUTH)
            self.assertEqual(resp.status_code, 200, resp.data)

        done = self.client.post(f'/api/uploads/chunked/{upload_uuid}/complete', headers=_AUTH)
        self.assertEqual(done.status_code, 201, done.data)
        data = done.get_json()
        self.assertEqual(data['slug'], 'chunked-disc')
        self.assertIn('queued_analyses', data)
        with self.app.app_context():
            artefact = Artefact.query.one()
            self.assertEqual(artefact.file_size, len(b'first-half;second-half'))
            key = f'uploads/{artefact.storage_path}'
            self.assertEqual(self.storage.files[key], b'first-half;second-half')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
