"""
Tests for the batch hash-database import endpoint
(POST /api/hash-databases/<db_id>/import) used by `arco hashdb import`.

The batch endpoint inserts every product and file in one request and runs the
collection-linking pass exactly once (rather than once per product, which made
the CLI import ~30s per product).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_batch_import -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-batch-import-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_MD5 = 'ab' * 16
_SHA1 = 'cd' * 20


class TestHashdbBatchImport(unittest.TestCase):

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
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            hdb = HashDatabase(name='Batch DB', file_count=0)
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id

            # An already-extracted, unlinked file that one of the imported
            # KnownFiles should match.
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
            ef = ExtractedFile(
                partition_id=part.id, path='!Foo/!RunImage', filename='!RunImage',
                md5=_MD5, sha1=_SHA1, file_size=123,
                is_directory=False, is_known=False)
            db.session.add(ef)
            db.session.flush()
            cls.ef_id = ef.id
            db.session.commit()

    def test_batch_import_creates_rows_links_once(self):
        import myapp.blueprints.api as api_mod
        from myapp.database import ExtractedFile, HashDatabase, KnownFile, KnownProduct

        # Spy on link_new_known_files to prove it runs exactly once for the
        # whole batch (not once per product).
        calls = {'n': 0}
        original = api_mod.link_new_known_files

        def _spy(database, new_kf_list):
            calls['n'] += 1
            return original(database, new_kf_list)

        api_mod.link_new_known_files = _spy
        try:
            resp = self.client.post(
                f'/api/hash-databases/{self.db_id}/import',
                json={'products': [
                    {'title': '!Foo', 'files': [
                        {'filename': '!RunImage', 'file_size': 123,
                         'md5': _MD5.upper(), 'sha1': _SHA1, 'is_required': True},
                        {'filename': '!Boot', 'md5': 'ef' * 16},
                    ]},
                    {'title': '!Bar', 'files': [
                        {'filename': 'data', 'sha256': '12' * 32},
                    ]},
                    {'title': '', 'files': [{'filename': 'skipped'}]},  # no title -> skipped
                ]},
                headers={'X-API-Key': _WORKER_KEY},
            )
        finally:
            api_mod.link_new_known_files = original

        self.assertEqual(resp.status_code, 201, resp.data)
        body = resp.get_json()
        self.assertEqual(body['products'], 2)   # blank-title product skipped
        self.assertEqual(body['files'], 3)
        self.assertEqual(calls['n'], 1)          # linking ran once for the batch

        with self.app.app_context():
            self.assertEqual(KnownProduct.query.filter_by(database_id=self.db_id).count(), 2)
            self.assertEqual(KnownFile.query.filter_by(database_id=self.db_id).count(), 3)
            hdb = self.db.session.get(HashDatabase, self.db_id)
            self.assertEqual(hdb.file_count, 3)

            # Hash stored lowercased; the matching collection file got linked.
            kf = KnownFile.query.filter_by(database_id=self.db_id, md5=_MD5).first()
            self.assertIsNotNone(kf)
            ef = self.db.session.get(ExtractedFile, self.ef_id)
            self.assertTrue(ef.is_known)
            self.assertEqual(ef.known_file_id, kf.id)

    def test_missing_products_rejected(self):
        resp = self.client.post(
            f'/api/hash-databases/{self.db_id}/import',
            json={},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_batch_import_can_skip_immediate_link(self):
        import myapp.blueprints.api as api_mod
        from myapp.database import HashDatabase

        with self.app.app_context():
            hdb = HashDatabase(name='No Immediate Link')
            self.db.session.add(hdb)
            self.db.session.commit()
            db_id = hdb.id

        calls = {'n': 0}
        original = api_mod.link_new_known_files

        def _spy(database, new_kf_list):
            calls['n'] += 1
            return original(database, new_kf_list)

        api_mod.link_new_known_files = _spy
        try:
            resp = self.client.post(
                f'/api/hash-databases/{db_id}/import',
                json={
                    'link': False,
                    'products': [
                        {'title': '!NoLink', 'files': [
                            {'filename': '!RunImage', 'file_size': 123, 'md5': _MD5},
                        ]},
                    ],
                },
                headers={'X-API-Key': _WORKER_KEY},
            )
        finally:
            api_mod.link_new_known_files = original

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(calls['n'], 0)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
