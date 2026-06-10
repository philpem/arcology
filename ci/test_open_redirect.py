"""
Tests for post-login open-redirect protection on the ``?next=`` parameter.

Both the local password login (``myapp/app.py`` ``login``) and the OIDC SSO
login (``myapp/blueprints/oidc_auth.py`` ``sso_login``) redirect the user to a
caller-supplied ``next`` URL.  The value must be confined to a same-origin
relative path, otherwise the trusted login page becomes an open redirect.

The previous guard — ``next.startswith('/') and not next.startswith('//')`` —
accepted values containing control characters and backslashes.  The concretely
exploitable vector on the current Werkzeug is a URL-encoded TAB:
``next=/%09/evil.com`` decodes to ``/\t/evil.com``, passes the old check, and
Werkzeug *strips* the TAB while finalising the Location header, yielding
``Location: //evil.com`` — a scheme-relative URL the browser loads as
``http://evil.com``.  (The backslash variant ``/\\evil.com`` is instead
percent-encoded to the same-origin ``/%5Cevil.com``, so it is not exploitable
on this version — which is exactly why the validator must reject the whole
class rather than rely on response-layer encoding.)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_open_redirect -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-open-redirect-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestIsSafeRedirectPath(unittest.TestCase):
    """Unit tests for the shared validator used by both login flows."""

    def setUp(self):
        from myapp.utils.safe_redirect import is_safe_redirect_path
        self.ok = is_safe_redirect_path

    def test_accepts_same_origin_paths(self):
        for good in ('/', '/dashboard', '/items/abc?sort=name_asc', '/a/b/c'):
            self.assertTrue(self.ok(good), good)

    def test_rejects_scheme_relative_and_absolute(self):
        for bad in ('//evil.com', 'http://evil.com', 'https://evil.com',
                    'javascript:alert(1)', 'data:text/html,x'):
            self.assertFalse(self.ok(bad), bad)

    def test_rejects_backslash_normalisation_bypass(self):
        # The core regression: browsers turn '\' into '/', so these escape origin.
        for bad in ('/\\evil.com', '/\\/evil.com', '\\\\evil.com', '/path\\x'):
            self.assertFalse(self.ok(bad), bad)

    def test_rejects_control_characters(self):
        for bad in ('/\tevil.com', '/\nevil.com', '/\revil.com', '/a\x00b'):
            self.assertFalse(self.ok(bad), bad)

    def test_rejects_empty_and_non_str(self):
        for bad in ('', None, 123, b'/x'):
            self.assertFalse(self.ok(bad), repr(bad))


class TestLoginRedirectEndToEnd(unittest.TestCase):
    """The /login handler must not honour a hostile next= value."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User, UserPermission
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            u = User(username='redir-user', is_admin=False,
                     permission=UserPermission.READ_WRITE)
            u.setPassword('correct horse battery staple')
            db.session.add(u)
            db.session.commit()

    def _login(self, next_value):
        return self.client.post(
            f'/login?next={next_value}',
            data={'username': 'redir-user',
                  'password': 'correct horse battery staple',
                  'submit': 'Log in'},
            follow_redirects=False,
        )

    def test_encoded_tab_bypass_does_not_redirect_offsite(self):
        # The real exploit: /%09/evil.com decodes to /\t/evil.com, passes the
        # old startswith('/') check, and Werkzeug strips the TAB to emit
        # Location: //evil.com (scheme-relative -> off-origin). The fix rejects
        # the control character, so the user lands on the dashboard instead.
        resp = self._login('/%09/evil.com')
        self.assertEqual(resp.status_code, 302, resp.data)
        location = resp.headers.get('Location', '')
        self.assertNotIn('evil.com', location)
        # Never a scheme-relative Location.
        self.assertFalse(location.startswith('//'), location)

    def test_backslash_bypass_does_not_redirect_offsite(self):
        resp = self._login('/\\evil.com')
        self.assertEqual(resp.status_code, 302, resp.data)
        location = resp.headers.get('Location', '')
        # After the fix the value is rejected outright (dashboard); it must not
        # appear as a raw or scheme-relative off-origin target.
        self.assertFalse(location.startswith('//'), location)
        self.assertNotIn('\\', location)

    def test_safe_next_is_honoured(self):
        resp = self._login('/items/abc')
        self.assertEqual(resp.status_code, 302, resp.data)
        self.assertTrue(resp.headers.get('Location', '').endswith('/items/abc'))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
