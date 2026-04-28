"""
Tests for the chunked upload API.

Exercises the four-endpoint protocol:
  POST /api/uploads/chunked/init
  POST /api/uploads/chunked/<uuid>/chunk/<n>
  GET  /api/uploads/chunked/<uuid>/status
  POST /api/uploads/chunked/<uuid>/complete

Uses SQLite in-memory + LocalStorage (temp dirs).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_chunked_upload -v
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-chunked-upload-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_AUTH = {'X-API-Key': _WORKER_KEY}


class TestChunkedUpload(unittest.TestCase):
    """Full protocol tests for chunked file upload."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        # Use separate temp dirs for uploads and outputs so tests are isolated
        cls._tmpdir = tempfile.mkdtemp(prefix='arcology-ci-chunked-')
        upload_dir = os.path.join(cls._tmpdir, 'uploads')
        output_dir = os.path.join(cls._tmpdir, 'outputs')
        os.makedirs(upload_dir)
        os.makedirs(output_dir)

        cls.app = create_app()
        cls.app.config.update({
            'TESTING': True,
            'UPLOAD_FOLDER': upload_dir,
            'OUTPUT_FOLDER': output_dir,
        })
        # Re-initialise storage with the updated paths so LocalStorage picks them up
        from shared.storage import create_storage
        storage_cfg = dict(cls.app.config)
        storage_cfg['UPLOAD_FOLDER'] = upload_dir
        storage_cfg['OUTPUT_FOLDER'] = output_dir
        with cls.app.app_context():
            cls.app.storage = create_storage(storage_cfg)

        cls.client = cls.app.test_client()
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()
            cls.item_uuid = cls._create_item(_db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    @classmethod
    def _create_item(cls, db):
        from myapp.database import Item, Platform
        platform = Platform(name='Test Platform Chunked')
        db.session.add(platform)
        db.session.flush()
        item = Item(name='Test Item Chunked', platform_id=platform.id)
        db.session.add(item)
        db.session.commit()
        return item.uuid

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init(self, filename='test.img', total_chunks=3, **extra):
        payload = {
            'filename': filename,
            'total_chunks': total_chunks,
            'item_uuid': self.item_uuid,
            'label': 'Test Artefact',
        }
        payload.update(extra)
        return self.client.post(
            '/api/uploads/chunked/init',
            data=json.dumps(payload),
            content_type='application/json',
            headers=_AUTH,
        )

    def _upload_chunk(self, upload_uuid, chunk_index, data):
        return self.client.post(
            f'/api/uploads/chunked/{upload_uuid}/chunk/{chunk_index}',
            data=data,
            content_type='application/octet-stream',
            headers=_AUTH,
        )

    def _status(self, upload_uuid):
        return self.client.get(
            f'/api/uploads/chunked/{upload_uuid}/status',
            headers=_AUTH,
        )

    def _complete(self, upload_uuid):
        return self.client.post(
            f'/api/uploads/chunked/{upload_uuid}/complete',
            headers=_AUTH,
        )

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_happy_path_creates_artefact(self):
        """Full protocol: init → 3 chunks → complete creates a valid Artefact."""
        # Build a known payload split into 3 chunks
        chunk_a = b'AAAA' * 256   # 1 KB
        chunk_b = b'BBBB' * 256
        chunk_c = b'CCCC' * 256
        full_data = chunk_a + chunk_b + chunk_c

        expected_md5 = hashlib.md5(full_data).hexdigest()
        expected_sha256 = hashlib.sha256(full_data).hexdigest()
        expected_size = len(full_data)

        # Init
        resp = self._init(filename='happy.img', total_chunks=3)
        self.assertEqual(resp.status_code, 201, resp.data)
        upload_uuid = resp.get_json()['upload_uuid']
        self.assertRegex(upload_uuid, r'^[0-9a-f]{32}$')

        # Upload chunks
        for idx, chunk in enumerate([chunk_a, chunk_b, chunk_c]):
            r = self._upload_chunk(upload_uuid, idx, chunk)
            self.assertEqual(r.status_code, 200, r.data)
            body = r.get_json()
            self.assertTrue(body['received'])
            self.assertEqual(body['chunk'], idx)

        # Complete
        resp = self._complete(upload_uuid)
        self.assertEqual(resp.status_code, 201, resp.data)
        result = resp.get_json()

        self.assertIn('uuid', result)
        self.assertEqual(result['md5'], expected_md5)
        self.assertEqual(result['sha256'], expected_sha256)
        self.assertEqual(result['file_size'], expected_size)
        self.assertEqual(result['original_filename'], 'happy.img')

    def test_happy_path_single_chunk(self):
        """A single-chunk upload (small file edge case) must also work."""
        data = b'single chunk content'
        resp = self._init(filename='single.adf', total_chunks=1)
        self.assertEqual(resp.status_code, 201)
        upload_uuid = resp.get_json()['upload_uuid']

        self._upload_chunk(upload_uuid, 0, data)
        resp = self._complete(upload_uuid)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.get_json()['file_size'], len(data))

    def test_chunk_dir_cleaned_up_after_complete(self):
        """Chunk temp directory must be removed after /complete."""
        resp = self._init(filename='cleanup.img', total_chunks=1)
        upload_uuid = resp.get_json()['upload_uuid']

        chunk_dir = os.path.join(self.app.instance_path, '.chunks', upload_uuid)
        self._upload_chunk(upload_uuid, 0, b'x' * 64)
        self._complete(upload_uuid)

        self.assertFalse(os.path.exists(chunk_dir),
                         f'Chunk dir still exists after complete: {chunk_dir}')

    # ------------------------------------------------------------------
    # Status endpoint
    # ------------------------------------------------------------------

    def test_status_shows_received_chunks(self):
        """/status returns which chunks have been received."""
        resp = self._init(filename='status.img', total_chunks=3)
        upload_uuid = resp.get_json()['upload_uuid']

        self._upload_chunk(upload_uuid, 0, b'chunk0')
        self._upload_chunk(upload_uuid, 2, b'chunk2')  # skip chunk 1

        resp = self._status(upload_uuid)
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertEqual(body['total_chunks'], 3)
        self.assertEqual(body['received_chunks'], [0, 2])

    def test_status_404_for_unknown_uuid(self):
        resp = self._status('a' * 32)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # Missing chunk detection
    # ------------------------------------------------------------------

    def test_complete_rejects_missing_chunks(self):
        """/complete returns 400 listing missing chunks."""
        resp = self._init(filename='missing.img', total_chunks=3)
        upload_uuid = resp.get_json()['upload_uuid']

        # Upload only chunk 0 and 2, skip chunk 1
        self._upload_chunk(upload_uuid, 0, b'chunk0')
        self._upload_chunk(upload_uuid, 2, b'chunk2')

        resp = self._complete(upload_uuid)
        self.assertEqual(resp.status_code, 400, resp.data)
        body = resp.get_json()
        self.assertIn('error', body)
        self.assertIn('1', body['error'])  # chunk 1 must be mentioned

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def test_init_requires_filename(self):
        resp = self.client.post(
            '/api/uploads/chunked/init',
            data=json.dumps({'total_chunks': 1, 'item_uuid': self.item_uuid, 'label': 'x'}),
            content_type='application/json',
            headers=_AUTH,
        )
        self.assertIn(resp.status_code, (400, 422), resp.data)

    def test_init_requires_valid_item(self):
        resp = self._init(filename='x.img', total_chunks=1, item_uuid='a' * 32)
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_chunk_rejects_invalid_upload_uuid(self):
        """A non-hex upload_uuid should return 404, not 500."""
        resp = self.client.post(
            '/api/uploads/chunked/../../etc/passwd/chunk/0',
            data=b'x',
            content_type='application/octet-stream',
            headers=_AUTH,
        )
        # Flask may 404 directly on routing; either way not 500
        self.assertNotEqual(resp.status_code, 500)

    def test_chunk_404_for_unknown_uuid(self):
        resp = self._upload_chunk('b' * 32, 0, b'data')
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_complete_404_for_unknown_uuid(self):
        resp = self._complete('c' * 32)
        self.assertEqual(resp.status_code, 404, resp.data)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def test_init_requires_auth(self):
        resp = self.client.post(
            '/api/uploads/chunked/init',
            data=json.dumps({'filename': 'f.img', 'total_chunks': 1,
                             'item_uuid': self.item_uuid, 'label': 'x'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_chunk_requires_auth(self):
        resp = self.client.post(
            f'/api/uploads/chunked/{"d" * 32}/chunk/0',
            data=b'x',
            content_type='application/octet-stream',
        )
        self.assertEqual(resp.status_code, 401, resp.data)

    # ------------------------------------------------------------------
    # Stale purge
    # ------------------------------------------------------------------

    def test_stale_chunk_dirs_are_purged_on_complete(self):
        """Chunk dirs older than 24 h are removed when /complete is called."""
        base = os.path.join(self.app.instance_path, '.chunks')
        os.makedirs(base, exist_ok=True)

        # Create a fake stale chunk dir
        stale_uuid = 'f' * 32
        stale_dir = os.path.join(base, stale_uuid)
        os.makedirs(stale_dir, exist_ok=True)
        # Back-date it by 25 h
        old_time = time.time() - (25 * 3600)
        os.utime(stale_dir, (old_time, old_time))

        # Trigger a successful complete on a real upload (which runs the purge)
        resp = self._init(filename='purge_trigger.img', total_chunks=1)
        upload_uuid = resp.get_json()['upload_uuid']
        self._upload_chunk(upload_uuid, 0, b'trigger')
        self._complete(upload_uuid)

        self.assertFalse(os.path.exists(stale_dir),
                         'Stale chunk dir was not purged')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
