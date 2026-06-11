"""
Tests for resource-exhaustion limits on the chunked upload API.

Hardening for four issues on /api/uploads/chunked/*:
  1. unbounded total_chunks -> /complete materialised a list of that size
     (memory-exhaustion DoS, triggerable with no chunks uploaded);
  2. chunk_index not bounded by total_chunks -> junk chunk files;
  3. no cap on the assembled size (chunked is the only path that can exceed
     the per-request MAX_CONTENT_LENGTH);
  4. sessions were not bound to their creating user.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_chunked_upload_limits -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-chunked-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_MAX_UPLOAD_SIZE = 1024  # small cap for cheap over-size tests


def _api_user(db, username):
    from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission
    u = User(username=username, password_hash='x',
             permission=UserPermission.READ_WRITE, can_use_api=True)
    db.session.add(u)
    db.session.flush()
    key, raw = ApiKey.create(user_id=u.id, name=f'{username}-k',
                             permission=ApiKeyPermission.READ_UPLOAD)
    db.session.add(key)
    db.session.commit()
    return raw


class TestChunkedUploadLimits(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.database import Item
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['MAX_UPLOAD_SIZE'] = _MAX_UPLOAD_SIZE
        cls.client = cls.app.test_client()
        cls.db = db

        cls._tmp = tempfile.TemporaryDirectory()
        cls.app.storage = LocalStorage(
            uploads_dir=Path(cls._tmp.name) / 'uploads',
            outputs_dir=Path(cls._tmp.name) / 'outputs')

        with cls.app.app_context():
            db.create_all()
            cls.key_a = _api_user(db, 'chunk-a')
            cls.key_b = _api_user(db, 'chunk-b')
            item = Item(name='pub-upload-item')  # public -> any uploader may add
            db.session.add(item)
            db.session.commit()
            cls.item_uuid = item.uuid

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _init(self, key, **over):
        body = {'filename': 'f.bin', 'total_chunks': 1,
                'item_uuid': self.item_uuid, 'label': 'L'}
        body.update(over)
        return self.client.post('/api/uploads/chunked/init',
                                headers={'X-API-Key': key}, json=body)

    # --- 1. total_chunks cap (the OOM vector) ---
    def test_init_rejects_excessive_total_chunks(self):
        from myapp.blueprints.api import _MAX_TOTAL_CHUNKS
        r = self._init(self.key_a, total_chunks=_MAX_TOTAL_CHUNKS + 1)
        self.assertEqual(r.status_code, 400, r.data)

    def test_complete_rejects_tampered_total_chunks_without_oom(self):
        # Write a meta.json with an enormous total_chunks directly, then call
        # /complete.  If the guard fires *before* building the per-chunk list,
        # this returns 400 instantly; without the guard it would OOM.
        import json
        import uuid as _uuid
        from myapp.blueprints.api import _chunk_dir
        with self.app.app_context():
            up = _uuid.uuid4().hex
            d = _chunk_dir(up)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'meta.json'), 'w') as f:
                json.dump({'total_chunks': 10 ** 9, 'item_uuid': self.item_uuid,
                           'filename': 'f.bin', 'label': 'L',
                           'creator_user_id': None}, f)
        r = self.client.post(f'/api/uploads/chunked/{up}/complete',
                            headers={'X-API-Key': self.key_a})
        self.assertEqual(r.status_code, 400, r.data)

    # --- 2. chunk_index bound ---
    def test_chunk_index_out_of_range_rejected(self):
        up = self._init(self.key_a, total_chunks=2).get_json()['upload_uuid']
        r = self.client.post(f'/api/uploads/chunked/{up}/chunk/5',
                            headers={'X-API-Key': self.key_a}, data=b'x')
        self.assertEqual(r.status_code, 400, r.data)

    # --- 3. assembled-size cap ---
    def test_init_rejects_oversize_declared_total_size(self):
        r = self._init(self.key_a, total_size=_MAX_UPLOAD_SIZE + 1)
        self.assertEqual(r.status_code, 413, r.data)

    def test_complete_rejects_oversize_assembly(self):
        up = self._init(self.key_a, total_chunks=1).get_json()['upload_uuid']
        # One chunk larger than the cap.
        self.client.post(f'/api/uploads/chunked/{up}/chunk/0',
                         headers={'X-API-Key': self.key_a},
                         data=b'A' * (_MAX_UPLOAD_SIZE + 10))
        r = self.client.post(f'/api/uploads/chunked/{up}/complete',
                            headers={'X-API-Key': self.key_a})
        self.assertEqual(r.status_code, 413, r.data)

    # --- 4. session bound to creator ---
    def test_other_user_cannot_touch_session(self):
        up = self._init(self.key_a, total_chunks=1).get_json()['upload_uuid']
        # User B may not upload chunks, read status, or complete user A's session.
        self.assertEqual(self.client.post(
            f'/api/uploads/chunked/{up}/chunk/0',
            headers={'X-API-Key': self.key_b}, data=b'x').status_code, 404)
        self.assertEqual(self.client.get(
            f'/api/uploads/chunked/{up}/status',
            headers={'X-API-Key': self.key_b}).status_code, 404)
        self.assertEqual(self.client.post(
            f'/api/uploads/chunked/{up}/complete',
            headers={'X-API-Key': self.key_b}).status_code, 404)

    # --- regression: the happy path still works ---
    def test_normal_chunked_upload_succeeds(self):
        up = self._init(self.key_a, total_chunks=2,
                        filename='ok.bin', label='OK').get_json()['upload_uuid']
        for i in range(2):
            self.assertEqual(self.client.post(
                f'/api/uploads/chunked/{up}/chunk/{i}',
                headers={'X-API-Key': self.key_a},
                data=b'D' * 100).status_code, 200)
        r = self.client.post(f'/api/uploads/chunked/{up}/complete',
                            headers={'X-API-Key': self.key_a})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.get_json()['file_size'], 200)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
