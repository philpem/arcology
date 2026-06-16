"""
Tests for User preferences and resolve_per_page utility.

Covers:
  - User.get_preference / set_preference model methods
  - JSON column round-trip through the database
  - resolve_per_page priority chain (explicit param → saved pref → config)
  - Automatic preference saving on explicit per_page selection
  - "View All" not saved as a preference
  - Invalid saved preference values fall back to config

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_user_preferences -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-prefs-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestUserPreferenceModel(unittest.TestCase):
    """Test get_preference / set_preference on the User model."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        cls.User = User

        with cls.app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Create a fresh user for each test
        self.user = self.User(username=f'testuser_{id(self)}', password_hash='x' * 72)
        self.db.session.add(self.user)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def test_get_preference_returns_default_when_none(self):
        self.assertIsNone(self.user.get_preference('per_page'))
        self.assertEqual(self.user.get_preference('per_page', 25), 25)

    def test_set_preference_initializes_from_none(self):
        self.assertIsNone(self.user.preferences)
        self.user.set_preference('per_page', 50)
        self.assertEqual(self.user.preferences, {'per_page': 50})

    def test_set_preference_preserves_other_keys(self):
        self.user.set_preference('per_page', 50)
        self.user.set_preference('theme', 'dark')
        self.assertEqual(self.user.get_preference('per_page'), 50)
        self.assertEqual(self.user.get_preference('theme'), 'dark')

    def test_set_preference_updates_existing_key(self):
        self.user.set_preference('per_page', 50)
        self.user.set_preference('per_page', 100)
        self.assertEqual(self.user.get_preference('per_page'), 100)

    def test_json_column_round_trips_through_db(self):
        self.user.set_preference('per_page', 100)
        self.db.session.commit()
        # Re-query from DB
        reloaded = self.db.session.get(self.User, self.user.id)
        self.assertEqual(reloaded.get_preference('per_page'), 100)


class TestResolvePerPage(unittest.TestCase):
    """Test the resolve_per_page utility function."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User
        from myapp.extensions import db
        from myapp.utils.pagination import VALID_PER_PAGE, resolve_per_page

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        cls.User = User
        cls.resolve_per_page = staticmethod(resolve_per_page)
        cls.VALID_PER_PAGE = VALID_PER_PAGE

        with cls.app.app_context():
            db.create_all()
            # Create a test user
            cls.test_user = User(username='pref_test_user', password_hash='x' * 72)
            db.session.add(cls.test_user)
            db.session.commit()
            cls.test_user_id = cls.test_user.id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()

    def _login_user(self, user):
        """Simulate Flask-Login for the test user."""
        from flask_login import login_user
        login_user(user)

    def test_explicit_param_used(self):
        """Explicit per_page query param takes priority."""
        with self.app.test_request_context('/?per_page=100'):
            user = self.db.session.get(self.User, self.test_user_id)
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(per_page, 100)
            self.assertFalse(view_all)

    def test_explicit_param_saves_preference(self):
        """Selecting a per_page value saves it to the user's preferences."""
        with self.app.test_request_context('/?per_page=50'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.preferences = None  # reset
            self.db.session.commit()
            self._login_user(user)
            self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(user.get_preference('per_page'), 50)

    def test_saved_preference_used_as_default(self):
        """When no per_page param, the saved preference is used."""
        with self.app.test_request_context('/'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.set_preference('per_page', 100)
            self.db.session.commit()
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(per_page, 100)

    def test_config_fallback_when_no_preference(self):
        """When no per_page param and no preference, config default is used."""
        with self.app.test_request_context('/'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.preferences = None
            self.db.session.commit()
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(per_page, 25)

    def test_view_all_not_saved(self):
        """per_page=0 (view all) should NOT be saved as a preference."""
        with self.app.test_request_context('/?per_page=0'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.set_preference('per_page', 50)
            self.db.session.commit()
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertTrue(view_all)
            self.assertEqual(per_page, 10000)
            self.assertEqual(page, 1)
            # Preference should still be 50
            self.assertEqual(user.get_preference('per_page'), 50)

    def test_invalid_saved_preference_falls_back_to_config(self):
        """An invalid stored preference value falls through to config."""
        with self.app.test_request_context('/'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.set_preference('per_page', 999)
            self.db.session.commit()
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(per_page, 25)

    def test_invalid_param_falls_back_to_preference(self):
        """An invalid per_page param falls through to saved preference."""
        with self.app.test_request_context('/?per_page=77'):
            user = self.db.session.get(self.User, self.test_user_id)
            user.set_preference('per_page', 100)
            self.db.session.commit()
            self._login_user(user)
            per_page, page, view_all = self.resolve_per_page('ITEMS_PER_PAGE', 25)
            self.assertEqual(per_page, 100)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
