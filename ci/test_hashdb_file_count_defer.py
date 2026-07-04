"""
Tests that HashDatabase.file_count is deferred and only loaded where displayed.

file_count is a correlated COUNT over known_files (millions of rows for a
NIST-scale DB).  It is deferred so management routes / _get_database_or_404
that never show the count don't pay for it, while the paths that DO show it
(the index listing, the /api serializer, the detail view) undefer it so the
listing stays a single query instead of an N+1.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_file_count_defer -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-filecount-defer-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestHashDbFileCountDefer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import HashDatabase, KnownFile, KnownProduct
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            db.create_all()
            hdb = HashDatabase(name='CountDB')
            db.session.add(hdb)
            db.session.flush()
            prod = KnownProduct(database_id=hdb.id, title='Prod')
            db.session.add(prod)
            db.session.flush()
            for i in range(3):
                db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                         filename=f'f{i}', md5=f'{i:032x}'))
            db.session.commit()
            cls.db_id = hdb.id

    def test_default_load_defers_file_count(self):
        from sqlalchemy import inspect
        from myapp.blueprints.hashdb import _get_database_or_404
        from myapp.database import HashDatabase
        with self.app.app_context():
            self.db.session.expunge_all()
            database = _get_database_or_404(self.db_id)
            # Not yet accessed → still unloaded (the deferred COUNT wasn't emitted).
            self.assertIn('file_count', inspect(database).unloaded)
            # And it's a real HashDatabase (the 404/soft-delete guard still works).
            self.assertIsInstance(database, HashDatabase)

    def test_with_file_count_loads_it(self):
        from sqlalchemy import inspect
        from myapp.blueprints.hashdb import _get_database_or_404
        with self.app.app_context():
            self.db.session.expunge_all()
            database = _get_database_or_404(self.db_id, with_file_count=True)
            self.assertNotIn('file_count', inspect(database).unloaded)
            self.assertEqual(database.file_count, 3)

    def test_api_listing_reports_count(self):
        resp = self.client.get('/api/hash-databases', headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200, resp.data)
        row = next(r for r in resp.get_json() if r['id'] == self.db_id)
        self.assertEqual(row['file_count'], 3)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
