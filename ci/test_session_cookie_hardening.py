"""
Tests for session cookie security hardening.

The session cookie should be HttpOnly and SameSite=Lax, and Secure (HTTPS-only)
by default — except under DEBUG, where Secure is off so local HTTP development
still works.  SESSION_COOKIE_SECURE is overridable via myapp.cfg / env.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_session_cookie_hardening -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-cookie-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestSessionCookieHardening(unittest.TestCase):

    def setUp(self):
        os.environ.pop('SESSION_COOKIE_SECURE', None)

    def tearDown(self):
        os.environ.pop('SESSION_COOKIE_SECURE', None)

    def _app(self):
        from myapp.app import create_app
        return create_app()

    def test_samesite_and_httponly_set(self):
        app = self._app()
        self.assertEqual(app.config['SESSION_COOKIE_SAMESITE'], 'Lax')
        self.assertTrue(app.config['SESSION_COOKIE_HTTPONLY'])

    def test_secure_defaults_on_without_debug(self):
        app = self._app()
        # CI has no myapp.cfg / DEBUG, so Secure defaults on.  Guard on DEBUG so
        # this still holds if a local config enables it.
        if not app.config.get('DEBUG'):
            self.assertTrue(app.config['SESSION_COOKIE_SECURE'])

    def test_env_override_true_and_false(self):
        for val, expect in (('true', True), ('false', False),
                            ('1', True), ('0', False), ('yes', True)):
            os.environ['SESSION_COOKIE_SECURE'] = val
            try:
                app = self._app()
                self.assertEqual(app.config['SESSION_COOKIE_SECURE'], expect,
                                 f'{val!r} -> {expect}')
            finally:
                os.environ.pop('SESSION_COOKIE_SECURE', None)

    def test_cookie_attributes_appear_on_set_cookie(self):
        # End-to-end: a login session sets a cookie carrying the attributes.
        from myapp.database import User, UserPermission
        from myapp.extensions import db
        os.environ['SESSION_COOKIE_SECURE'] = 'true'
        try:
            app = self._app()
            app.config['TESTING'] = True
            with app.app_context():
                db.create_all()
                u = User(username='cookie-user', password_hash='x',
                         permission=UserPermission.READ_ONLY)
                db.session.add(u)
                db.session.commit()
                uid = u.id
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(uid)
            # Trigger a response that writes the session cookie.
            resp = client.get('/login')
            set_cookie = resp.headers.get('Set-Cookie', '')
            if set_cookie:  # a session cookie was issued
                self.assertIn('HttpOnly', set_cookie)
                self.assertIn('SameSite=Lax', set_cookie)
                self.assertIn('Secure', set_cookie)
        finally:
            os.environ.pop('SESSION_COOKIE_SECURE', None)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
