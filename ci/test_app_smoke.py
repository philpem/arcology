"""
Flask application smoke tests.

Creates the app with a SQLite in-memory database (no PostgreSQL needed) and
exercises the most critical runtime behaviour:

  - The app starts and the database schema can be created
  - The unauthenticated /api/health endpoint returns 200
  - Protected endpoints return 401 when no key is supplied
  - Protected endpoints return 401 when a wrong key is supplied
  - Protected endpoints are reachable with the pre-shared WORKER_API_KEY

These tests are not a substitute for full integration tests against PostgreSQL,
but they catch broken imports, misconfigured extensions, and authentication
regressions without needing a running database server.

Environment variables used (also set by the app-tests CI job):
    SQLALCHEMY_DATABASE_URI  — defaults to sqlite:///:memory:
    SECRET_KEY               — defaults to a fixed test value
    WORKER_API_KEY           — defaults to 'ci-test-worker-key'

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_app_smoke -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Set environment variables before importing the app so create_app() picks
# them up. CI sets these via the job's ``env:`` block; local runs use defaults.
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-smoke-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestAppSmoke(unittest.TestCase):
    """Smoke tests that verify the Flask app starts and auth works."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        cls.client = cls.app.test_client()

        # Create schema directly from models (bypasses Alembic migrations).
        # This is intentional: we want to test the app, not the migration chain.
        with cls.app.app_context():
            db.create_all()

    # ------------------------------------------------------------------
    # Health check (unauthenticated)
    # ------------------------------------------------------------------

    def test_health_returns_200(self):
        """GET /api/health should return 200 without credentials."""
        resp = self.client.get('/api/health')
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_health_returns_json(self):
        resp = self.client.get('/api/health')
        data = resp.get_json()
        self.assertIsNotNone(data, 'Response is not JSON')
        self.assertIn('status', data)

    # ------------------------------------------------------------------
    # Authentication: no credentials
    # ------------------------------------------------------------------

    def test_protected_endpoint_no_auth_returns_401(self):
        """GET /api/items without credentials should return 401."""
        resp = self.client.get('/api/items')
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_protected_endpoint_no_auth_returns_json_error(self):
        resp = self.client.get('/api/items')
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertIn('error', data)

    # ------------------------------------------------------------------
    # Authentication: wrong key
    # ------------------------------------------------------------------

    def test_protected_endpoint_wrong_key_returns_401(self):
        """GET /api/items with a bad key should return 401."""
        resp = self.client.get('/api/items', headers={'X-API-Key': 'wrong-key'})
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_protected_endpoint_bearer_wrong_key_returns_401(self):
        resp = self.client.get('/api/items', headers={'Authorization': 'Bearer wrong-key'})
        self.assertEqual(resp.status_code, 401, resp.data)

    # ------------------------------------------------------------------
    # Authentication: valid WORKER_API_KEY
    # ------------------------------------------------------------------

    def test_protected_endpoint_worker_key_accepted(self):
        """GET /api/items with the worker key should not return 401 or 500."""
        resp = self.client.get('/api/items', headers={'X-API-Key': _WORKER_KEY})
        self.assertNotIn(
            resp.status_code, (401, 403, 500),
            f'Unexpected status {resp.status_code}: {resp.data!r}',
        )

    def test_protected_endpoint_worker_key_bearer_accepted(self):
        resp = self.client.get(
            '/api/items',
            headers={'Authorization': f'Bearer {_WORKER_KEY}'},
        )
        self.assertNotIn(
            resp.status_code, (401, 403, 500),
            f'Unexpected status {resp.status_code}: {resp.data!r}',
        )

    def test_protected_endpoint_worker_key_returns_json(self):
        resp = self.client.get('/api/items', headers={'X-API-Key': _WORKER_KEY})
        data = resp.get_json()
        self.assertIsNotNone(data, 'Response is not JSON')

    # ------------------------------------------------------------------
    # Analysis pending endpoint (worker-facing)
    # ------------------------------------------------------------------

    def test_analysis_pending_worker_key_accepted(self):
        """GET /api/analysis/pending is a key worker endpoint; should not 401/500."""
        resp = self.client.get('/api/analysis/pending', headers={'X-API-Key': _WORKER_KEY})
        self.assertNotIn(
            resp.status_code, (401, 403, 500),
            f'Unexpected status {resp.status_code}: {resp.data!r}',
        )


class TestApiKeyLastUsedAtTimezone(unittest.TestCase):
    """Regression test for timezone-naive/aware datetime mismatch in API key auth.

    When an ApiKey has a non-NULL last_used_at (stored as a timezone-naive
    datetime by SQLAlchemy's DateTime column), the auth middleware must not
    raise ``TypeError: can't subtract offset-naive and offset-aware datetimes``.
    """

    @classmethod
    def setUpClass(cls):
        from datetime import datetime, timedelta
        from myapp.app import create_app
        from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.create_all()

            # Create a user that may use the API
            user = User(
                username='test-tz-user',
                password_hash='x',   # never used for auth in this test
                can_use_api=True,
                permission=UserPermission.READ_WRITE,
            )
            db.session.add(user)
            db.session.flush()

            # Create an API key and record the raw value
            key_obj, cls.raw_key = ApiKey.create(
                user_id=user.id,
                name='tz-regression-key',
                permission=ApiKeyPermission.READ_ONLY,
            )
            # Simulate a key that was used before: set last_used_at to a
            # timezone-naive datetime (as SQLAlchemy DateTime returns from the DB).
            key_obj.last_used_at = datetime.utcnow() - timedelta(seconds=120)
            db.session.add(key_obj)
            db.session.commit()

    def test_request_with_stale_last_used_at_does_not_500(self):
        """GET /api/items with a key that has a naive last_used_at must not return 500."""
        resp = self.client.get('/api/items', headers={'X-API-Key': self.raw_key})
        self.assertNotEqual(
            resp.status_code, 500,
            f'Got 500 (likely timezone mismatch in auth middleware): {resp.data!r}',
        )

    def test_request_with_stale_last_used_at_returns_200(self):
        """GET /api/items with a valid user key should return 200."""
        resp = self.client.get('/api/items', headers={'X-API-Key': self.raw_key})
        self.assertEqual(
            resp.status_code, 200,
            f'Expected 200 but got {resp.status_code}: {resp.data!r}',
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
