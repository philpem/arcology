"""
Tests that SSO login cannot silently take over a local account by username.

_get_or_create_user() links a first-time SSO login onto an existing account
matched by OIDC_MATCH_CLAIM (default preferred_username) and overwrites its
password hash.  If the IdP lets users choose their own preferred_username, an
attacker could present preferred_username='admin' and seize the local admin
account.  Linking onto a pre-existing LOCAL account by a username claim is now
gated behind OIDC_LINK_BY_USERNAME (default off); the verified-email path and
re-linking an already-SSO-managed account stay allowed.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_oidc_account_linking -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-oidc-link-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestOidcAccountLinking(unittest.TestCase):

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.db = db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _local_user(self, username, *, email=None, is_admin=False):
        from myapp.database import User, UserPermission
        u = User(username=username, email=email, password_hash='real-bcrypt-hash',
                 is_admin=is_admin, permission=UserPermission.READ_WRITE,
                 oidc_managed=False)
        self.db.session.add(u)
        self.db.session.commit()
        return u

    def test_username_link_refused_when_disabled(self):
        from myapp.blueprints.oidc_auth import _get_or_create_user
        self.app.config['OIDC_LINK_BY_USERNAME'] = False
        victim = self._local_user('admin', is_admin=True)
        user, err = _get_or_create_user({'sub': 'attacker-sub', 'preferred_username': 'admin'})
        self.assertIsNone(user)
        self.assertIsNotNone(err)
        # The local account is untouched — not hijacked, password intact.
        self.db.session.refresh(victim)
        self.assertIsNone(victim.oidc_sub)
        self.assertEqual(victim.password_hash, 'real-bcrypt-hash')
        self.assertFalse(victim.oidc_managed)

    def test_username_link_allowed_when_enabled(self):
        from myapp.blueprints.oidc_auth import _get_or_create_user
        self.app.config['OIDC_LINK_BY_USERNAME'] = True
        self._local_user('alice')
        user, err = _get_or_create_user({'sub': 'alice-sub', 'preferred_username': 'alice'})
        self.assertIsNone(err)
        self.assertIsNotNone(user)
        self.assertEqual(user.oidc_sub, 'alice-sub')
        self.assertTrue(user.oidc_managed)
        self.assertEqual(user.password_hash, '!')

    def test_verified_email_link_allowed_when_username_link_disabled(self):
        from myapp.blueprints.oidc_auth import _get_or_create_user
        self.app.config['OIDC_LINK_BY_USERNAME'] = False
        self.app.config['OIDC_MATCH_CLAIM'] = 'email'
        self._local_user('bob', email='bob@example.com')
        user, err = _get_or_create_user({
            'sub': 'bob-sub', 'email': 'bob@example.com', 'email_verified': True})
        self.assertIsNone(err)
        self.assertIsNotNone(user)
        self.assertEqual(user.oidc_sub, 'bob-sub')

    def test_relink_sso_managed_account_allowed_when_disabled(self):
        # An account already SSO-managed (password sentinel '!') is not a local
        # takeover target, so a username match may re-link it even with the flag off.
        from myapp.blueprints.oidc_auth import _get_or_create_user
        from myapp.database import User, UserPermission
        self.app.config['OIDC_LINK_BY_USERNAME'] = False
        managed = User(username='carol', password_hash='!', oidc_managed=True,
                       permission=UserPermission.READ_ONLY)
        self.db.session.add(managed)
        self.db.session.commit()
        user, err = _get_or_create_user({'sub': 'carol-new-sub', 'preferred_username': 'carol'})
        self.assertIsNone(err)
        self.assertIsNotNone(user)
        self.assertEqual(user.oidc_sub, 'carol-new-sub')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
