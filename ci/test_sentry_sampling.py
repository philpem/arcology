"""
Sentry performance tracing must keep human request latency at full fidelity
while refusing to spend the span quota on machine-to-machine polling.

Background: over one billing cycle the three polling endpoints below emitted
96.2% of all spans (the worker poll loop alone was 91.7%), exhausting a 5M/month
span quota roughly ten days early -- every month.  Human traffic, the part
actually worth tracing, was under 4%.

Covers:
  * polling endpoints and static assets sample at 0.0;
  * the browser UI pollers sample at a low-but-nonzero rate, so their queries
    stay visible for latency without scaling with open tabs;
  * everything else keeps the full default rate, including db spans;
  * the sampler matches on the WSGI path, not the dotted transaction name;
  * the rates are configurable, and profiling defaults off.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_sentry_sampling -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-sentry-sampling-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _ctx(path):
    """A sampling_context shaped like the one the WSGI integration passes."""
    return {'wsgi_environ': {'PATH_INFO': path}}


class TestTracesSampler(unittest.TestCase):

    def setUp(self):
        from myapp.app import build_traces_sampler
        self.sampler = build_traces_sampler({})

    def test_paths_with_nothing_to_measure_are_never_traced(self):
        """A bare `SELECT 1` healthcheck and static files have no query to watch."""
        for path in ('/api/health',
                     '/static/css/site.css',
                     '/static/js/site.js'):
            with self.subTest(path=path):
                self.assertEqual(self.sampler(_ctx(path)), 0.0)

    def test_poll_endpoints_are_sampled_thinly_not_dropped(self):
        """These do real DB work, and run tens of thousands of times a day.

        A query regression on the poll loop is expensive precisely because of
        that frequency, so keep a thin sample rather than going blind.
        """
        for path in ('/api/analysis/pending',
                     '/api/analysis/reset-stale'):
            with self.subTest(path=path):
                rate = self.sampler(_ctx(path))
                self.assertGreater(rate, 0.0, 'poll latency must stay visible')
                self.assertLessEqual(
                    rate, 0.05,
                    'poll volume is ~51k/day; a high rate re-floods the quota')

    def test_untraced_paths_ignore_the_poll_rate(self):
        """Raising the poll rate must not start tracing healthcheck/static."""
        from myapp.app import build_traces_sampler
        sampler = build_traces_sampler({'SENTRY_POLL_TRACES_SAMPLE_RATE': 1.0})
        self.assertEqual(sampler(_ctx('/api/health')), 0.0)
        self.assertEqual(sampler(_ctx('/static/css/site.css')), 0.0)
        self.assertEqual(sampler(_ctx('/api/analysis/pending')), 1.0)

    def test_ui_pollers_are_sampled_not_dropped(self):
        """Kept visible for latency, but capped so open tabs can't dominate."""
        for path in ('/stats.json',
                     '/analysis/queue/status.json',
                     '/analysis/0123456789abcdef/status.json',
                     '/artefacts/0123456789abcdef/analysis-status.json',
                     '/hashdb/status.json'):
            with self.subTest(path=path):
                rate = self.sampler(_ctx(path))
                self.assertGreater(rate, 0.0, 'UI poller latency must stay visible')
                self.assertLess(rate, 1.0, 'UI pollers must not scale with open tabs')

    def test_human_traffic_keeps_full_fidelity(self):
        """Everything a person actually waits on, incl. its db spans."""
        for path in ('/',
                     '/items/',
                     '/artefacts/0123456789abcdef',
                     '/artefacts/0123456789abcdef/viewer',
                     '/search?q=acorn',
                     '/analysis/queue',
                     '/api/analysis/123',
                     '/api/artefacts/0123456789abcdef'):
            with self.subTest(path=path):
                self.assertEqual(self.sampler(_ctx(path)), 1.0)

    def test_api_analysis_detail_is_not_caught_by_the_pending_prefix(self):
        """/api/analysis/<id> must survive the /api/analysis/pending prefix."""
        self.assertLess(self.sampler(_ctx('/api/analysis/pending')), 1.0)
        self.assertEqual(self.sampler(_ctx('/api/analysis/123')), 1.0)
        self.assertEqual(self.sampler(_ctx('/api/analyses')), 1.0)

    def test_matches_wsgi_path_not_dotted_transaction_name(self):
        """Regression guard for the way this is easiest to get wrong.

        The Sentry UI shows 'myapp_blueprints_api.get_pending_analyses', but
        that name is assigned *after* sampling.  A sampler written against it
        would pass review, ship, and silently change nothing.
        """
        dotted = {'transaction_context':
                  {'name': 'myapp_blueprints_api.get_pending_analyses'}}
        # No wsgi_environ at all: must not blow up, and must not match.
        self.assertEqual(self.sampler(dotted), 1.0)
        self.assertEqual(self.sampler({}), 1.0)

    def test_rates_are_configurable(self):
        from myapp.app import build_traces_sampler
        sampler = build_traces_sampler({
            'SENTRY_TRACES_SAMPLE_RATE': 0.5,
            'SENTRY_POLL_TRACES_SAMPLE_RATE': 0.001,
            'SENTRY_UI_POLL_TRACES_SAMPLE_RATE': 0.25,
        })
        self.assertEqual(sampler(_ctx('/api/analysis/pending')), 0.001)
        self.assertEqual(sampler(_ctx('/stats.json')), 0.25)
        self.assertEqual(sampler(_ctx('/artefacts/abc')), 0.5)
        self.assertEqual(sampler(_ctx('/api/health')), 0.0)

    def test_new_rate_keys_are_env_overridable(self):
        """The typed env loader must know about the new float keys."""
        from myapp import app as app_module
        for key in ('SENTRY_POLL_TRACES_SAMPLE_RATE',
                    'SENTRY_UI_POLL_TRACES_SAMPLE_RATE'):
            with self.subTest(key=key):
                self.assertIn(key, app_module._ENV_FLOAT_KEYS)

    def test_worker_override_keys_use_one_spelling(self):
        """The web app must not register keys the worker doesn't read.

        The two halves once spelled these in opposite orders
        (WORKER_SENTRY_DSN vs SENTRY_WORKER_DSN), leaving three keys that were
        loaded into app.config and then consulted by nothing.
        """
        from myapp import app as app_module
        registered = set()
        for group in ('_ENV_STR_KEYS', '_ENV_BOOL_KEYS', '_ENV_INT_KEYS',
                      '_ENV_FLOAT_KEYS', '_ENV_JSON_KEYS', '_ENV_BYTE_SIZE_KEYS'):
            registered.update(getattr(app_module, group))
        stale = {k for k in registered if k.startswith('WORKER_SENTRY_')}
        self.assertFalse(
            stale, f'dead worker Sentry keys registered by the web app: {sorted(stale)}')


class TestSentryInitWiring(unittest.TestCase):
    """The sampler is only worth anything if init() actually receives it."""

    def _captured_init_kwargs(self, **env):
        import types
        from unittest import mock

        captured = {}

        def fake_init(**kwargs):
            captured.update(kwargs)

        fake_sentry = types.ModuleType('sentry_sdk')
        fake_sentry.init = fake_init
        fake_sentry.capture_exception = lambda *a, **k: None
        fake_sentry.capture_message = lambda *a, **k: None

        flask_mod = types.ModuleType('sentry_sdk.integrations.flask')
        flask_mod.FlaskIntegration = lambda *a, **k: object()
        sa_mod = types.ModuleType('sentry_sdk.integrations.sqlalchemy')
        sa_mod.SqlalchemyIntegration = lambda *a, **k: object()

        modules = {
            'sentry_sdk': fake_sentry,
            'sentry_sdk.integrations.flask': flask_mod,
            'sentry_sdk.integrations.sqlalchemy': sa_mod,
        }

        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            with mock.patch.dict(sys.modules, modules):
                from myapp.app import create_app
                create_app()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return captured

    def test_init_uses_sampler_and_disables_profiling(self):
        kwargs = self._captured_init_kwargs(
            SENTRY_DSN='https://public@example.ingest.sentry.io/1')

        # A flat rate alongside a sampler is ambiguous; the sampler must be the
        # only trace-sampling input.
        self.assertNotIn('traces_sample_rate', kwargs)
        self.assertIn('traces_sampler', kwargs)

        sampler = kwargs['traces_sampler']
        self.assertEqual(sampler(_ctx('/api/health')), 0.0)
        self.assertEqual(sampler(_ctx('/api/analysis/pending')), 0.01)
        self.assertEqual(sampler(_ctx('/artefacts/abc')), 1.0)

        # Profiling bills against a separate, previously-exhausted quota.
        self.assertEqual(kwargs['profiles_sample_rate'], 0.0)

    def test_profiling_can_be_re_enabled_explicitly(self):
        kwargs = self._captured_init_kwargs(
            SENTRY_DSN='https://public@example.ingest.sentry.io/1',
            SENTRY_PROFILES_SAMPLE_RATE='0.1')
        self.assertEqual(kwargs['profiles_sample_rate'], 0.1)

    def test_send_default_pii_defaults_on_and_is_overridable(self):
        dsn = 'https://public@example.ingest.sentry.io/1'
        self.assertIs(
            self._captured_init_kwargs(SENTRY_DSN=dsn)['send_default_pii'], True)
        self.assertIs(
            self._captured_init_kwargs(
                SENTRY_DSN=dsn,
                SENTRY_SEND_DEFAULT_PII='false')['send_default_pii'], False)
        # A typo must fail closed rather than silently enabling PII.
        self.assertIs(
            self._captured_init_kwargs(
                SENTRY_DSN=dsn,
                SENTRY_SEND_DEFAULT_PII='ture')['send_default_pii'], False)


class TestWorkerSentryConfig(unittest.TestCase):
    """The worker reads its own env; it must agree with the web app."""

    def _worker_setting(self, name, **env):
        """Reload the worker config under *env* and return one setting.

        importlib.reload() mutates the module in place, so the value has to be
        read before the restoring reload in the finally block -- returning the
        module itself would hand back post-restore values.
        """
        import importlib
        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            from worker.arcworker import config as worker_config
            importlib.reload(worker_config)
            return getattr(worker_config, name)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            from worker.arcworker import config as worker_config
            importlib.reload(worker_config)

    def test_send_default_pii_defaults_on(self):
        self.assertIs(self._worker_setting('SENTRY_SEND_DEFAULT_PII'), True)

    def test_send_default_pii_can_be_disabled(self):
        self.assertIs(
            self._worker_setting('SENTRY_SEND_DEFAULT_PII',
                                 SENTRY_SEND_DEFAULT_PII='false'), False)

    def test_zero_trace_rate_survives_the_or_fallback(self):
        """'0' is a truthy string, so the `or` chain must not skip past it."""
        self.assertEqual(
            self._worker_setting('SENTRY_TRACES_SAMPLE_RATE',
                                 SENTRY_WORKER_TRACES_SAMPLE_RATE='0',
                                 SENTRY_TRACES_SAMPLE_RATE='1.0'), 0.0)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
