"""
Tests for administrator-managed API keys.

Covers the admin key-management page added in issue #745:

  - The admin API key page is gated to administrators
  - An administrator can view a user's active keys
  - An administrator can create a key on a user's behalf and is shown the raw
    value exactly once
  - Keys are refused for accounts without API access, including SSO-managed
    accounts (API access for those remains gated on the OIDC role)
  - An administrator can revoke a user's key
  - An administrator can manage their own keys via the dedicated page (the
    edit-user page deliberately blocks self-editing)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_admin_api_keys -v
"""

import os
import re
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-admin-api-keys-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestAdminApiKeys(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User, UserPermission
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            admin = User(
                username='key-admin', password_hash='x', is_admin=True,
                permission=UserPermission.READ_WRITE, can_use_api=True,
            )
            target = User(
                username='key-target', password_hash='x',
                permission=UserPermission.READ_WRITE, can_use_api=True,
            )
            no_api = User(
                username='key-no-api', password_hash='x',
                permission=UserPermission.READ_WRITE, can_use_api=False,
            )
            # SSO-managed account without API access: roles gate API access.
            sso_no_api = User(
                username='key-sso-no-api', password_hash='!', oidc_managed=True,
                permission=UserPermission.READ_WRITE, can_use_api=False,
            )
            # SSO-managed account that has been granted the API role.
            sso_with_api = User(
                username='key-sso-with-api', password_hash='!', oidc_managed=True,
                permission=UserPermission.READ_WRITE, can_use_api=True,
            )
            read_only = User(
                username='key-read-only', password_hash='x',
                permission=UserPermission.READ_ONLY, can_use_api=True,
            )
            db.session.add_all([admin, target, no_api, sso_no_api, sso_with_api, read_only])
            db.session.commit()

            cls.admin_id = admin.id
            cls.target_id = target.id
            cls.no_api_id = no_api.id
            cls.sso_no_api_id = sso_no_api.id
            cls.sso_with_api_id = sso_with_api.id
            cls.read_only_id = read_only.id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _client_for(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        return client

    def _active_key_count(self, user_id):
        from myapp.database import ApiKey
        with self.app.app_context():
            return ApiKey.query.filter_by(user_id=user_id, is_active=True).count()

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def test_non_admin_cannot_view_key_management(self):
        client = self._client_for(self.target_id)
        resp = client.get(f'/admin/users/{self.target_id}/keys')
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_admin_can_view_key_management(self):
        client = self._client_for(self.admin_id)
        resp = client.get(f'/admin/users/{self.target_id}/keys')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn(b'key-target', resp.data)

    def test_admin_can_manage_own_keys(self):
        """The dedicated page works for self, unlike the edit-user page."""
        client = self._client_for(self.admin_id)
        resp = client.get(f'/admin/users/{self.admin_id}/keys')
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_admin_pages_render_with_key_links(self):
        """Admin index and edit-user page link to key management without error."""
        client = self._client_for(self.admin_id)

        index = client.get('/admin/')
        self.assertEqual(index.status_code, 200, index.data)
        self.assertIn(f'/admin/users/{self.target_id}/keys'.encode(), index.data)

        edit = client.get(f'/admin/users/{self.target_id}/edit')
        self.assertEqual(edit.status_code, 200, edit.data)
        self.assertIn(f'/admin/users/{self.target_id}/keys'.encode(), edit.data)

    def test_read_only_account_shows_permission_caution(self):
        """A read-only account's key form warns that keys will be capped."""
        client = self._client_for(self.admin_id)

        read_only = client.get(f'/admin/users/{self.read_only_id}/keys')
        self.assertEqual(read_only.status_code, 200, read_only.data)
        self.assertIn(b'capped at Read Only', read_only.data)

        read_write = client.get(f'/admin/users/{self.target_id}/keys')
        self.assertEqual(read_write.status_code, 200, read_write.data)
        self.assertNotIn(b'capped at Read Only', read_write.data)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def test_admin_can_create_key_for_user(self):
        client = self._client_for(self.admin_id)
        before = self._active_key_count(self.target_id)

        resp = client.post(
            f'/admin/users/{self.target_id}/keys/create',
            data={'name': 'lab-key', 'permission': 'read_upload'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)
        self.assertTrue(resp.headers['Location'].endswith('/keys/created'))

        self.assertEqual(self._active_key_count(self.target_id), before + 1)

        # The raw key is shown exactly once on the follow-up page.
        page = client.get(resp.headers['Location'])
        self.assertEqual(page.status_code, 200, page.data)
        m = re.search(rb'(arc_[0-9a-f]{64})', page.data)
        self.assertIsNotNone(m, 'raw API key not shown on creation page')
        raw_key = m.group(1).decode()

        from myapp.database import ApiKey
        with self.app.app_context():
            key = ApiKey.verify(raw_key)
            self.assertIsNotNone(key, 'newly created key does not authenticate')
            self.assertEqual(key.name, 'lab-key')

        # Reloading the one-time page must 404 (raw key consumed from session).
        again = client.get(resp.headers['Location'])
        self.assertEqual(again.status_code, 404, again.data)

    def test_create_refused_without_api_access(self):
        client = self._client_for(self.admin_id)
        before = self._active_key_count(self.no_api_id)

        resp = client.post(
            f'/admin/users/{self.no_api_id}/keys/create',
            data={'name': 'should-fail', 'permission': 'read_only'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)
        self.assertEqual(self._active_key_count(self.no_api_id), before)

    def test_create_refused_for_sso_without_api_access(self):
        """API access for SSO accounts stays gated on the OIDC role."""
        client = self._client_for(self.admin_id)
        before = self._active_key_count(self.sso_no_api_id)

        resp = client.post(
            f'/admin/users/{self.sso_no_api_id}/keys/create',
            data={'name': 'should-fail', 'permission': 'read_only'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)
        self.assertEqual(self._active_key_count(self.sso_no_api_id), before)

    def test_admin_can_create_key_for_sso_with_api_access(self):
        client = self._client_for(self.admin_id)
        before = self._active_key_count(self.sso_with_api_id)

        resp = client.post(
            f'/admin/users/{self.sso_with_api_id}/keys/create',
            data={'name': 'sso-key', 'permission': 'read_only'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)
        self.assertEqual(self._active_key_count(self.sso_with_api_id), before + 1)

    def test_created_page_is_bound_to_key_owner(self):
        """The one-time display page refuses a key that belongs to another user."""
        client = self._client_for(self.admin_id)
        resp = client.post(
            f'/admin/users/{self.target_id}/keys/create',
            data={'name': 'bound-key', 'permission': 'read_only'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)

        # A different account's created URL must not reveal the pending key.
        wrong = client.get(f'/admin/users/{self.admin_id}/keys/created')
        self.assertEqual(wrong.status_code, 404, wrong.data)

        # The mismatch must not consume the key, so the owner's page still works.
        right = client.get(f'/admin/users/{self.target_id}/keys/created')
        self.assertEqual(right.status_code, 200, right.data)
        self.assertIn(b'arc_', right.data)

    def test_profile_key_flow_still_works(self):
        """The self-service profile key display still works after the refactor."""
        client = self._client_for(self.admin_id)
        resp = client.post(
            '/profile/keys/create',
            data={'name': 'self-key', 'permission': 'read_only'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)

        page = client.get('/profile/keys/created')
        self.assertEqual(page.status_code, 200, page.data)
        self.assertIn(b'arc_', page.data)

        # The one-time page is consumed on first view.
        again = client.get('/profile/keys/created')
        self.assertEqual(again.status_code, 404, again.data)

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def test_admin_can_revoke_user_key(self):
        from myapp.database import ApiKey, ApiKeyPermission
        with self.app.app_context():
            key, _raw = ApiKey.create(
                user_id=self.target_id, name='revoke-me',
                permission=ApiKeyPermission.READ_ONLY,
            )
            self.db.session.add(key)
            self.db.session.commit()
            key_id = key.id

        client = self._client_for(self.admin_id)
        resp = client.post(
            f'/admin/users/{self.target_id}/keys/{key_id}/revoke',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, resp.data)

        with self.app.app_context():
            key = self.db.session.get(ApiKey, key_id)
            self.assertFalse(key.is_active)

    def test_cannot_revoke_another_users_key_via_wrong_user_id(self):
        from myapp.database import ApiKey, ApiKeyPermission
        with self.app.app_context():
            key, _raw = ApiKey.create(
                user_id=self.target_id, name='target-only',
                permission=ApiKeyPermission.READ_ONLY,
            )
            self.db.session.add(key)
            self.db.session.commit()
            key_id = key.id

        client = self._client_for(self.admin_id)
        # Mismatched user_id and key_id must 404, not revoke.
        resp = client.post(
            f'/admin/users/{self.no_api_id}/keys/{key_id}/revoke',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 404, resp.data)

        with self.app.app_context():
            key = self.db.session.get(ApiKey, key_id)
            self.assertTrue(key.is_active)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
