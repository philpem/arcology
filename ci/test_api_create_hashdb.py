"""
Tests for POST /api/artefacts/<uuid>/create-hashdb — the ingest-time hashdb
snapshot endpoint (myapp/blueprints/api.py).

Covers:
  - snapshotting an artefact's extracted files into a new database;
  - the exclude_from_similarity parameter is honoured (not hard-coded);
  - name is required; a duplicate name returns 409;
  - authentication is enforced.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_api_create_hashdb -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-create-hashdb-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
_WORKER_KEY = os.environ['WORKER_API_KEY']


def _h(seed: str) -> str:
    return (seed * 64)[:64]


class TestCreateHashdbApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        from myapp.database import (
            Artefact,
            ExtractedFile,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        for model in (ExtractedFile, KnownFile, KnownProduct, HashDatabase,
                      Partition, Artefact, Item):
            model.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _artefact_with_files(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, ExtractedFile, FilesystemType, Item, Partition

        item = Item(name='Install discs')
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id, label='Install',
            artefact_type=ArtefactType.RAW_SECTOR,
            original_filename='install.img', storage_path='uploads/install.img',
        )
        self.db.session.add(art)
        self.db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0,
                         filesystem=FilesystemType.ADFS)
        self.db.session.add(part)
        self.db.session.flush()
        for name in ('App.foo', 'App.bar', 'App.baz'):
            self.db.session.add(ExtractedFile(
                partition_id=part.id, path=name, filename=name.split('.')[-1],
                file_size=100, is_directory=False, sha256=_h(name)))
        self.db.session.commit()
        return art

    def _post(self, uuid, body, key=_WORKER_KEY):
        headers = {'X-API-Key': key} if key else {}
        return self.client.post(
            f'/api/artefacts/{uuid}/create-hashdb', json=body, headers=headers)

    def test_creates_database_from_artefact(self):
        from myapp.database import HashDatabase
        art = self._artefact_with_files()
        resp = self._post(art.uuid, {'name': 'RISC OS 3.6 snapshot',
                                     'exclude_from_similarity': True})
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.get_json()
        self.assertEqual(data['files_added'], 3)
        self.assertTrue(data['exclude_from_similarity'])
        hdb = self.db.session.get(HashDatabase, data['id'])
        self.assertIsNotNone(hdb)
        self.assertTrue(hdb.exclude_from_similarity)
        self.assertEqual(hdb.name, 'RISC OS 3.6 snapshot')

    def test_exclude_defaults_false(self):
        """Commercial-software snapshots should not be excluded unless asked."""
        art = self._artefact_with_files()
        resp = self._post(art.uuid, {'name': 'Impression Publisher'})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(resp.get_json()['exclude_from_similarity'])

    def test_name_required(self):
        art = self._artefact_with_files()
        resp = self._post(art.uuid, {'exclude_from_similarity': True})
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_duplicate_name_conflict(self):
        art = self._artefact_with_files()
        first = self._post(art.uuid, {'name': 'Dup'})
        self.assertEqual(first.status_code, 201, first.data)
        second = self._post(art.uuid, {'name': 'Dup'})
        self.assertEqual(second.status_code, 409, second.data)

    def test_requires_auth(self):
        art = self._artefact_with_files()
        resp = self._post(art.uuid, {'name': 'NoAuth'}, key=None)
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_unknown_artefact_404(self):
        resp = self._post('deadbeef' * 4, {'name': 'X'})
        self.assertEqual(resp.status_code, 404, resp.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
