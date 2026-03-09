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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
