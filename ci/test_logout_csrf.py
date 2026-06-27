"""
Tests that logout is POST-only (forced-logout CSRF protection).

A GET-accessible logout can be triggered cross-site (e.g. an <img> or a link),
forcing a victim to be logged out.  Logout must be a POST driven by the
CSRF-protected navbar form, so a cross-site GET cannot end the session.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_logout_csrf -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-logout-csrf-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestLogoutCsrf(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User, UserPermission
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        # Keep CSRF enabled so the POST path is exercised realistically.
        cls.app.config['WTF_CSRF_ENABLED'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            u = User(username='logout-user', password_hash='x',
                     permission=UserPermission.READ_ONLY)
            db.session.add(u)
            db.session.commit()
            cls.uid = u.id

    def _client_logged_in(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True
        return client

    def test_get_logout_is_rejected(self):
        client = self._client_logged_in()
        r = client.get('/logout')
        # Method not allowed — a cross-site GET cannot force logout.
        self.assertEqual(r.status_code, 405, r.data)

    def test_post_logout_without_token_is_rejected(self):
        client = self._client_logged_in()
        r = client.post('/logout')  # no CSRF token
        self.assertIn(r.status_code, (400, 403), r.data)

    def test_post_logout_with_token_succeeds(self):
        client = self._client_logged_in()
        # Fetch a page to obtain a CSRF token from the logout form.
        import re
        page = client.get('/').data.decode()
        m = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(m, 'no csrf_token found on page')
        r = client.post('/logout', data={'csrf_token': m.group(1)})
        self.assertEqual(r.status_code, 302, r.data)
        # Session is cleared: a follow-up authenticated-only page redirects to login.
        follow = client.get('/profile/')
        self.assertEqual(follow.status_code, 302)
        self.assertIn('/login', follow.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
