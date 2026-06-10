"""
Tests that POST /api/analysis/reset-stale is restricted to worker or staff+.

reset_stale_analyses() re-queues every RUNNING analysis stuck past the stale
timeout back to PENDING — system-wide, including jobs on private artefacts the
caller cannot see.  It is legitimately called by the worker on startup (crash
recovery) and by staff operators via the UI, but an ordinary read_write user
must not be able to disrupt the global analysis queue.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_reset_stale_auth -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-reset-stale-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


def _key(db, username, perm):
    from myapp.database import ApiKey, ApiKeyPermission, User
    u = User(username=username, password_hash='x', permission=perm, can_use_api=True)
    db.session.add(u)
    db.session.flush()
    key, raw = ApiKey.create(user_id=u.id, name=f'{username}-k',
                             permission=ApiKeyPermission.READ_WRITE)
    db.session.add(key)
    db.session.commit()
    return raw


class TestResetStaleAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import UserPermission
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls.rw_key = _key(db, 'rs-rw', UserPermission.READ_WRITE)
            cls.staff_key = _key(db, 'rs-staff', UserPermission.STAFF)

    def _post(self, key):
        return self.client.post('/api/analysis/reset-stale', headers={'X-API-Key': key})

    def test_read_write_user_forbidden(self):
        self.assertEqual(self._post(self.rw_key).status_code, 403)

    def test_staff_user_allowed(self):
        self.assertEqual(self._post(self.staff_key).status_code, 200)

    def test_worker_allowed(self):
        self.assertEqual(self._post(_WORKER_KEY).status_code, 200)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
