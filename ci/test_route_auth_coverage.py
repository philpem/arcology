"""
Route auth-coverage sweep.

Walks the whole URL map and asserts that, with PUBLIC_MODE off, no GET route
serves a 200 to an anonymous caller unless it is on an explicit allowlist of
intentionally-public endpoints.  Everything else must redirect to login (302) or
reject (401/403/404).

This catches the recurring "forgot the auth decorator on a new route" bug class
that per-feature tests miss: add a GET route without @login_required /
@require_auth / @public_readable and this sweep goes red until you either guard
it or consciously add it to the allowlist below.

Scope: GET routes only (the read-leak class).  Write-only routes and the
PUBLIC_MODE-on behaviour of @public_readable routes are covered elsewhere
(test_public_mode.py).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_route_auth_coverage -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-route-auth-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

# Endpoints that are intentionally reachable by an anonymous caller with
# PUBLIC_MODE off.  Keep this list SMALL and deliberate: a new entry here is a
# conscious decision to expose a route without authentication.
_PUBLIC_ENDPOINTS = {
    'auth.login',                                   # login form
    'myapp_blueprints_api.health_check',            # unauthenticated liveness probe
    'myapp_blueprints_api_docs.openapi_json',       # API documentation
    'myapp_blueprints_api_docs.openapi_yaml',
    'myapp_blueprints_api_docs.swagger_ui',
    'myapp_blueprints_dashboard.about',             # static info page
    'myapp_blueprints_help.index',                  # static help page
}


def _build_url(url_adapter, rule):
    """Build a concrete URL for *rule*, substituting dummy converter values.

    Tries string dummies first, then integer dummies (for <int:...> args).
    Returns None if the rule can't be built with simple placeholders.
    """
    for dummy in ('x', '1'):
        args = {arg: dummy for arg in rule.arguments}
        try:
            return url_adapter.build(rule.endpoint, args, method='GET')
        except Exception:  # noqa: S112 — expected: this dummy type didn't fit the converter, try the next
            continue
    return None


class TestRouteAuthCoverage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['PUBLIC_MODE'] = False
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            db.create_all()

    def test_no_get_route_serves_200_to_anonymous(self):
        adapter = self.app.url_map.bind('localhost')
        leaked = []
        checked = 0
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == 'static' or rule.endpoint.endswith('.static'):
                continue
            if 'GET' not in (rule.methods or set()):
                continue
            if rule.endpoint in _PUBLIC_ENDPOINTS:
                continue
            url = _build_url(adapter, rule)
            if url is None:
                continue
            checked += 1
            resp = self.client.get(url)
            if resp.status_code == 200:
                leaked.append(f'{rule.endpoint} ({url}) -> 200')
        self.assertGreater(checked, 20, 'sweep exercised too few routes — check URL building')
        self.assertEqual(
            leaked, [],
            'These GET routes served 200 to an anonymous caller with PUBLIC_MODE '
            'off. Add the missing auth decorator, or (if truly public) add the '
            'endpoint to _PUBLIC_ENDPOINTS:\n  ' + '\n  '.join(leaked),
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
