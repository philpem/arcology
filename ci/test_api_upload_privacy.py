"""
Tests that the REST upload endpoints honour the documented is_private flag.

The single-file upload (POST /api/items/<uuid>/artefacts/upload) and the
chunked upload both accept is_private, but previously dropped it — so a client
asking for a private artefact on a public item silently got a world-visible
one.  These tests pin the flag through both paths.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_api_upload_privacy -v
"""

import io
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
os.environ.setdefault('SECRET_KEY', 'ci-api-upload-privacy-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_AUTH = {'X-API-Key': os.environ['WORKER_API_KEY']}


class TestApiUploadPrivacy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.storage import create_storage
        from myapp.app import create_app
        from myapp.extensions import db

        cls._tmpdir = tempfile.mkdtemp(prefix='arcology-ci-upload-priv-')
        upload_dir = os.path.join(cls._tmpdir, 'uploads')
        output_dir = os.path.join(cls._tmpdir, 'outputs')
        os.makedirs(upload_dir)
        os.makedirs(output_dir)

        cls.app = create_app()
        cls.app.config.update({'TESTING': True, 'UPLOAD_FOLDER': upload_dir,
                               'OUTPUT_FOLDER': output_dir})
        storage_cfg = dict(cls.app.config)
        storage_cfg['UPLOAD_FOLDER'] = upload_dir
        storage_cfg['OUTPUT_FOLDER'] = output_dir
        with cls.app.app_context():
            cls.app.storage = create_storage(storage_cfg)
            db.create_all()
            from myapp.database import Item
            item = Item(name='Public Item')
            db.session.add(item)
            db.session.commit()
            cls.item_uuid = item.uuid
        cls.db = db
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _artefact(self, label):
        from myapp.database import Artefact
        with self.app.app_context():
            return Artefact.query.filter_by(label=label).first()

    # ---- single-file upload -------------------------------------------------

    def _upload(self, label, **fields):
        data = {'file': (io.BytesIO(b'payload-bytes'), f'{label}.bin'), 'label': label}
        data.update(fields)
        return self.client.post(
            f'/api/items/{self.item_uuid}/artefacts/upload',
            data=data, content_type='multipart/form-data', headers=_AUTH)

    def test_single_upload_private_true(self):
        resp = self._upload('SinglePriv', is_private='true', auto_analyse='false')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(self._artefact('SinglePriv').is_private)

    def test_single_upload_defaults_public(self):
        resp = self._upload('SinglePub', auto_analyse='false')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(self._artefact('SinglePub').is_private)

    # ---- chunked upload -----------------------------------------------------

    def _chunked_upload(self, label, chunk=b'chunk-bytes', **fields):
        payload = {'filename': f'{label}.bin', 'total_chunks': 1,
                   'item_uuid': self.item_uuid, 'label': label, 'auto_analyse': False}
        payload.update(fields)
        r = self.client.post('/api/uploads/chunked/init', data=json.dumps(payload),
                             content_type='application/json', headers=_AUTH)
        self.assertEqual(r.status_code, 201, r.data)
        uid = r.get_json()['upload_uuid']
        self.client.post(f'/api/uploads/chunked/{uid}/chunk/0', data=chunk,
                         content_type='application/octet-stream', headers=_AUTH)
        r = self.client.post(f'/api/uploads/chunked/{uid}/complete', headers=_AUTH)
        self.assertEqual(r.status_code, 201, r.data)

    def test_chunked_upload_private_true(self):
        self._chunked_upload('ChunkPriv', is_private=True)
        self.assertTrue(self._artefact('ChunkPriv').is_private)

    def test_chunked_upload_defaults_public(self):
        self._chunked_upload('ChunkPub')
        self.assertFalse(self._artefact('ChunkPub').is_private)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
