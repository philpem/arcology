"""
Tests for CSV formula-injection neutralisation and safe export filenames on
the hashdb export route.

Known-product fields (title, filename, description, relative_path) are free-text
supplied by read_write users.  A value beginning with =, +, -, @ (or tab/CR)
makes Excel/LibreOffice treat the cell as a formula, so it would execute when
another user opens the exported CSV (CWE-1236).  The export must neutralise
these.  The Content-Disposition filename, built from the (user-controlled)
database name, must also be free of quote/header-breakout characters.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_csv_injection -v
"""

import csv
import io
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-csv-inj-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_PAYLOAD = '=cmd|\'/c calc\'!A1'


class TestUnitCsvSafe(unittest.TestCase):
    def test_neutralises_formula_triggers(self):
        from myapp.blueprints.hashdb import _csv_safe
        for bad in ('=1+1', '+1', '-1', '@SUM(A1)', '\tx', '\rx'):
            self.assertEqual(_csv_safe(bad), "'" + bad)
        # Benign values pass through untouched.
        for ok in ('Elite', 'game.bin', 'deadbeef', '1024', ''):
            self.assertEqual(_csv_safe(ok), ok)

    def test_safe_download_name(self):
        from myapp.blueprints.hashdb import _safe_download_name
        out = _safe_download_name('evil".pdf\r\nX-Injected: 1', '.csv')
        for ch in ('"', '\r', '\n'):
            self.assertNotIn(ch, out)
        self.assertTrue(out.endswith('.csv'))


class TestExportEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import HashDatabase, KnownFile, KnownProduct, User, UserPermission
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            u = User(username='csv-user', password_hash='x',
                     permission=UserPermission.READ_WRITE)
            db.session.add(u)
            db.session.flush()
            cls.uid = u.id
            hdb = HashDatabase(name='evil"name')
            db.session.add(hdb)
            db.session.flush()
            cls.db_id = hdb.id
            prod = KnownProduct(database_id=hdb.id, title=_PAYLOAD)
            db.session.add(prod)
            db.session.flush()
            db.session.add(KnownFile(database_id=hdb.id, product_id=prod.id,
                                     filename='@evil.bin', md5='aa' * 16))
            db.session.commit()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True

    def test_csv_cells_are_neutralised(self):
        self._login()
        r = self.client.get(f'/hashdb/{self.db_id}/export?format=csv')
        self.assertEqual(r.status_code, 200, r.data)
        rows = list(csv.reader(io.StringIO(r.data.decode())))
        # Find the data row for the injected product.
        data_cells = [c for row in rows[1:] for c in row]
        # The raw formula and raw @filename must NOT appear; the quoted forms must.
        self.assertNotIn(_PAYLOAD, data_cells)
        self.assertIn("'" + _PAYLOAD, data_cells)
        self.assertIn("'@evil.bin", data_cells)

    def test_disposition_filename_is_safe(self):
        self._login()
        r = self.client.get(f'/hashdb/{self.db_id}/export?format=csv')
        cd = r.headers.get('Content-Disposition', '')
        # The db name 'evil"name' must not break the quoted filename.
        self.assertNotIn('evil"name', cd)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
