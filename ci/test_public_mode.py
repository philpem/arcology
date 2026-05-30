"""
Tests for PUBLIC_MODE anonymous access and the STAFF permission tier.

Covers:
  - PUBLIC_MODE off: anonymous GET of read routes redirects to login (302).
  - PUBLIC_MODE on: anonymous can GET public items/search but NOT private ones
    (private items return 404, excluded from lists).
  - PUBLIC_MODE on: anonymous POST/edit/delete/upload still 302.
  - PUBLIC_DOWNLOADS off: anonymous download returns 302 even in PUBLIC_MODE.
  - PUBLIC_DOWNLOADS on (default): anonymous download succeeds for public artefacts.
  - STAFF tier: has_permission('read_write') is True; has_permission('staff') True;
    READ_WRITE does not satisfy 'staff'.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_public_mode -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-public-mode-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _make_app(**config):
    from myapp.app import create_app
    from myapp.extensions import db as _db
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    for k, v in config.items():
        app.config[k] = v
    with app.app_context():
        _db.create_all()
    return app


def _make_user(db, username, *, permission, is_admin=False):
    from myapp.database import User, UserPermission
    user = User(
        username=username,
        password_hash='x',
        is_admin=is_admin,
        permission=UserPermission(permission),
    )
    db.session.add(user)
    db.session.flush()
    return user


def _make_item(db, name, *, owner=None, is_private=False):
    from myapp.database import Item
    from myapp.utils.privacy import recompute_item_privacy
    item = Item(name=name, is_private=is_private, owner_id=owner.id if owner else None)
    db.session.add(item)
    db.session.flush()
    recompute_item_privacy(item)
    db.session.commit()
    return item


# =============================================================================
# STAFF tier unit tests
# =============================================================================

class TestStaffPermissionTier(unittest.TestCase):
    """UserPermission.STAFF ordering and has_permission() semantics."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db
        self.app = _make_app()
        self.db = db

    def test_staff_enum_value(self):
        from myapp.database import UserPermission
        self.assertEqual(UserPermission.STAFF.value, 'staff')

    def test_staff_satisfies_read_write(self):
        from myapp.database import User, UserPermission
        with self.app.app_context():
            u = User(username='s', password_hash='x',
                     permission=UserPermission.STAFF)
            self.assertTrue(u.has_permission(UserPermission.READ_WRITE))

    def test_staff_satisfies_read_only(self):
        from myapp.database import User, UserPermission
        with self.app.app_context():
            u = User(username='s2', password_hash='x',
                     permission=UserPermission.STAFF)
            self.assertTrue(u.has_permission(UserPermission.READ_ONLY))

    def test_staff_satisfies_staff(self):
        from myapp.database import User, UserPermission
        with self.app.app_context():
            u = User(username='s3', password_hash='x',
                     permission=UserPermission.STAFF)
            self.assertTrue(u.has_permission(UserPermission.STAFF))

    def test_read_write_does_not_satisfy_staff(self):
        from myapp.database import User, UserPermission
        with self.app.app_context():
            u = User(username='rw', password_hash='x',
                     permission=UserPermission.READ_WRITE)
            self.assertFalse(u.has_permission(UserPermission.STAFF))

    def test_read_only_does_not_satisfy_staff(self):
        from myapp.database import User, UserPermission
        with self.app.app_context():
            u = User(username='ro', password_hash='x',
                     permission=UserPermission.READ_ONLY)
            self.assertFalse(u.has_permission(UserPermission.STAFF))


# =============================================================================
# PUBLIC_MODE off — anonymous access must redirect to login
# =============================================================================

class TestPublicModeOff(unittest.TestCase):
    """When PUBLIC_MODE is off (default), anonymous GETs redirect to login."""

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app(PUBLIC_MODE=False)
        cls.client = cls.app.test_client()

    def _assert_redirect_to_login(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 302, f'{path} should redirect anonymous user')
        self.assertIn('/login', resp.headers['Location'],
                      f'{path} redirect should point to login')

    def test_dashboard_requires_login(self):
        self._assert_redirect_to_login('/')

    def test_items_list_requires_login(self):
        self._assert_redirect_to_login('/items/')

    def test_search_requires_login(self):
        self._assert_redirect_to_login('/search/')

    def test_taxonomy_requires_login(self):
        self._assert_redirect_to_login('/taxonomy/')


# =============================================================================
# PUBLIC_MODE on — anonymous can browse public content
# =============================================================================

class TestPublicModeOn(unittest.TestCase):
    """When PUBLIC_MODE is on, anonymous users can browse non-private content."""

    @classmethod
    def setUpClass(cls):
        from myapp.extensions import db
        cls.app = _make_app(PUBLIC_MODE=True, PUBLIC_DOWNLOADS=True)
        cls.db = db
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            owner = _make_user(db, 'owner-pub', permission='read_write')
            cls.public_item = _make_item(db, 'Public Item', owner=owner, is_private=False)
            cls.private_item = _make_item(db, 'Private Item', owner=owner, is_private=True)
            cls.public_item_id = cls.public_item.url_id
            cls.private_item_id = cls.private_item.url_id

    def test_dashboard_accessible_anonymous(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)

    def test_items_list_accessible_anonymous(self):
        resp = self.client.get('/items/')
        self.assertEqual(resp.status_code, 200)

    def test_search_accessible_anonymous(self):
        resp = self.client.get('/search/')
        self.assertEqual(resp.status_code, 200)

    def test_taxonomy_accessible_anonymous(self):
        resp = self.client.get('/taxonomy/')
        self.assertEqual(resp.status_code, 200)

    def test_public_item_accessible_anonymous(self):
        resp = self.client.get(f'/items/{self.public_item_id}')
        self.assertIn(resp.status_code, (200, 301, 302),
                      'Public item should be accessible or redirect (slug canonicalisation)')

    def test_private_item_hidden_from_anonymous(self):
        resp = self.client.get(f'/items/{self.private_item_id}')
        self.assertEqual(resp.status_code, 404, 'Private item should 404 for anonymous user')

    def test_public_item_appears_in_list(self):
        resp = self.client.get('/items/')
        self.assertIn(b'Public Item', resp.data)

    def test_private_item_absent_from_list(self):
        resp = self.client.get('/items/')
        self.assertNotIn(b'Private Item', resp.data)

    def test_new_item_requires_login(self):
        resp = self.client.get('/items/new')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_post_new_item_requires_login(self):
        resp = self.client.post('/items/new', data={'name': 'hax'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])


# =============================================================================
# PUBLIC_DOWNLOADS off — downloads blocked even in PUBLIC_MODE
# =============================================================================

class TestPublicDownloadsOff(unittest.TestCase):
    """PUBLIC_DOWNLOADS=False blocks anonymous downloads even when PUBLIC_MODE=True."""

    @classmethod
    def setUpClass(cls):
        from myapp.extensions import db
        cls.app = _make_app(PUBLIC_MODE=True, PUBLIC_DOWNLOADS=False)
        cls.db = db
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            owner = _make_user(db, 'owner-dl', permission='read_write')
            item = _make_item(db, 'DL Test Item', owner=owner, is_private=False)

            from myapp.database import Artefact, ArtefactType, StorageDirectory
            art = Artefact(
                item_id=item.id,
                label='test.scp',
                original_filename='test.scp',
                artefact_type=ArtefactType.SCP,
                storage_directory=StorageDirectory.UPLOADS,
                storage_path='test.scp',
                file_size=0,
                owner_id=owner.id,
            )
            db.session.add(art)
            db.session.commit()
            cls.item_id = item.url_id
            cls.artefact_uuid = art.uuid

    def test_download_blocked_when_downloads_off(self):
        resp = self.client.get(f'/artefacts/{self.artefact_uuid}/download')
        self.assertEqual(resp.status_code, 302,
                         'Download should redirect to login when PUBLIC_DOWNLOADS=False')
        self.assertIn('/login', resp.headers['Location'])

    def test_view_still_accessible_when_downloads_off(self):
        resp = self.client.get(f'/items/{self.item_id}')
        self.assertIn(resp.status_code, (200, 301, 302),
                      'Item view should still be accessible with PUBLIC_DOWNLOADS=False')


# =============================================================================
# PUBLIC_MODE on + PUBLIC_DOWNLOADS on — downloads work for public artefacts
# =============================================================================

class TestPublicDownloadsOn(unittest.TestCase):
    """With PUBLIC_MODE+PUBLIC_DOWNLOADS=True, anonymous download is permitted."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from myapp.extensions import db
        cls.tmpdir = tempfile.mkdtemp()
        cls.app = _make_app(
            PUBLIC_MODE=True,
            PUBLIC_DOWNLOADS=True,
            STORAGE_BACKEND='local',
            UPLOAD_FOLDER=cls.tmpdir,
            OUTPUT_FOLDER=cls.tmpdir,
        )
        cls.db = db
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            owner = _make_user(db, 'owner-dl2', permission='read_write')
            item = _make_item(db, 'Download Item', owner=owner, is_private=False)

            from myapp.database import Artefact, ArtefactType, StorageDirectory
            import uuid as uuid_mod
            storage_path = f'{uuid_mod.uuid4().hex}.scp'
            file_path = os.path.join(cls.tmpdir, storage_path)
            with open(file_path, 'wb') as f:
                f.write(b'fake scp data')

            art = Artefact(
                item_id=item.id,
                label='test.scp',
                original_filename='test.scp',
                artefact_type=ArtefactType.SCP,
                storage_directory=StorageDirectory.UPLOADS,
                storage_path=storage_path,
                file_size=13,
                owner_id=owner.id,
            )
            db.session.add(art)
            db.session.commit()
            cls.artefact_uuid = art.uuid

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_download_allowed_for_public_artefact(self):
        resp = self.client.get(f'/artefacts/{self.artefact_uuid}/download')
        self.assertNotEqual(resp.status_code, 302,
                            'Anonymous should not be redirected to login for public artefact download')
        self.assertNotIn('/login', resp.headers.get('Location', ''),
                         'Download should not redirect to login in PUBLIC_MODE')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
