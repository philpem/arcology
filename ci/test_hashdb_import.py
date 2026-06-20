"""
Tests for the batch hash-database import endpoint's deduplication, so that
re-importing or merging (arco hashdb import --merge) into an existing database
does not create duplicate products and files.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_import -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-import-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestHashdbImportDedup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import HashDatabase
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            hdb = HashDatabase(name='Import DB', is_active=False)
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id
            db.session.commit()

    def _import(self, products, link=False):
        return self.client.post(
            f'/api/hash-databases/{self.db_id}/import',
            json={'products': products, 'link': link},
            headers={'X-API-Key': _WORKER_KEY})

    def _payload(self):
        return [{
            'title': '!Foo',
            'files': [
                {'filename': '!Run', 'md5': 'aa' * 16, 'relative_path': '!Foo'},
                {'filename': 'Data', 'md5': 'bb' * 16, 'relative_path': '!Foo'},
            ],
        }]

    def test_reimport_is_deduplicated(self):
        from myapp.database import KnownFile, KnownProduct

        first = self._import(self._payload())
        self.assertEqual(first.status_code, 201, first.data)
        body = first.get_json()
        self.assertEqual(body['products'], 1)
        self.assertEqual(body['files'], 2)

        # Re-importing the identical payload adds nothing new.
        second = self._import(self._payload())
        self.assertEqual(second.status_code, 201, second.data)
        body = second.get_json()
        self.assertEqual(body['products'], 0)
        self.assertEqual(body['products_reused'], 1)
        self.assertEqual(body['files'], 0)

        with self.app.app_context():
            self.assertEqual(
                KnownProduct.query.filter_by(database_id=self.db_id).count(), 1)
            self.assertEqual(
                KnownFile.query.filter_by(database_id=self.db_id).count(), 2)

    def test_merge_adds_only_new_files_to_existing_product(self):
        from myapp.database import KnownFile, KnownProduct

        with self.app.app_context():
            from myapp.database import HashDatabase
            hdb = HashDatabase(name='Merge DB', is_active=False)
            self.db.session.add(hdb)
            self.db.session.flush()
            merge_id = hdb.id
            self.db.session.commit()

        def imp(products):
            return self.client.post(
                f'/api/hash-databases/{merge_id}/import',
                json={'products': products, 'link': False},
                headers={'X-API-Key': _WORKER_KEY})

        imp([{'title': '!Bar', 'files': [
            {'filename': '!Run', 'md5': 'cc' * 16}]}])
        # Same product, one duplicate file and one new file.
        resp = imp([{'title': '!Bar', 'files': [
            {'filename': '!Run', 'md5': 'cc' * 16},      # dup
            {'filename': 'New', 'md5': 'dd' * 16},        # new
        ]}])
        body = resp.get_json()
        self.assertEqual(body['products'], 0)
        self.assertEqual(body['products_reused'], 1)
        self.assertEqual(body['files'], 1)

        with self.app.app_context():
            self.assertEqual(
                KnownProduct.query.filter_by(database_id=merge_id).count(), 1)
            self.assertEqual(
                KnownFile.query.filter_by(database_id=merge_id).count(), 2)

    def test_within_payload_duplicates_collapsed(self):
        from myapp.database import HashDatabase, KnownFile

        with self.app.app_context():
            hdb = HashDatabase(name='Dup DB', is_active=False)
            self.db.session.add(hdb)
            self.db.session.flush()
            dup_id = hdb.id
            self.db.session.commit()

        resp = self.client.post(
            f'/api/hash-databases/{dup_id}/import',
            json={'products': [{'title': '!Dup', 'files': [
                {'filename': 'A', 'md5': 'ee' * 16},
                {'filename': 'A', 'md5': 'ee' * 16},  # identical dup in same payload
            ]}], 'link': False},
            headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.get_json()['files'], 1)
        with self.app.app_context():
            self.assertEqual(
                KnownFile.query.filter_by(database_id=dup_id).count(), 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
