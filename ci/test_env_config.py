"""
myapp.cfg must be fully optional in Docker: every setting documented in
myapp.cfg.example has to be overridable from the environment so the file can be
deleted with no ill effects.

Covers:
  * the typed env loader coerces values to the right Python type;
  * an explicit empty CSP_HEADER is honoured (disables the header) while other
    empty string vars are treated as unset;
  * a coverage guard — every key in myapp.cfg.example appears in one of the
    env key groups in myapp/app.py;
  * the sibling worker knob (API_TIMEOUT) is read from the environment too.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_env_config -v
"""

import os
import re
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-env-config-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_CFG_EXAMPLE = os.path.join(_REPO_ROOT, 'myapp', 'myapp.cfg.example')


class TestEnvConfig(unittest.TestCase):

    def _build_app_with_env(self, **env):
        from myapp.app import create_app
        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            return create_app()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_typed_coercion_from_env(self):
        app = self._build_app_with_env(
            WORKER_STEP_DEADLINE_SECONDS='7',
            PUBLIC_MODE='true',
            SENTRY_TRACES_SAMPLE_RATE='0.25',
            # An option valid for SQLite's StaticPool (pool_size is not).
            SQLALCHEMY_ENGINE_OPTIONS='{"echo": false}',
        )
        self.assertEqual(app.config['WORKER_STEP_DEADLINE_SECONDS'], 7)
        self.assertIs(app.config['PUBLIC_MODE'], True)
        self.assertEqual(app.config['SENTRY_TRACES_SAMPLE_RATE'], 0.25)
        self.assertEqual(app.config['SQLALCHEMY_ENGINE_OPTIONS'], {'echo': False})

    def test_empty_csp_header_disables_but_other_empties_ignored(self):
        app = self._build_app_with_env(CSP_HEADER='', OIDC_DISCOVERY_URL='')
        # CSP_HEADER='' is meaningful (disable); it must land in config as ''.
        self.assertEqual(app.config.get('CSP_HEADER'), '')
        # A passed-but-empty ordinary string var is treated as unset.
        self.assertNotEqual(app.config.get('OIDC_DISCOVERY_URL'), '')

    def test_every_cfg_example_key_is_env_overridable(self):
        from myapp import app as app_module

        covered = set()
        for group in ('_ENV_STR_KEYS', '_ENV_BOOL_KEYS', '_ENV_INT_KEYS',
                      '_ENV_FLOAT_KEYS', '_ENV_JSON_KEYS'):
            covered.update(getattr(app_module, group))

        key_re = re.compile(r'^#?\s*([A-Z][A-Z0-9_]+)\s*=')
        with open(_CFG_EXAMPLE, encoding='utf-8') as fh:
            cfg_keys = {m.group(1) for line in fh
                        if (m := key_re.match(line))}

        self.assertTrue(cfg_keys, 'failed to parse any keys from myapp.cfg.example')
        missing = cfg_keys - covered
        self.assertFalse(
            missing,
            f'myapp.cfg.example keys not overridable from env (add them to a '
            f'key group in myapp/app.py): {sorted(missing)}')

    def test_worker_api_timeout_from_env(self):
        import importlib

        old = os.environ.get('API_TIMEOUT')
        os.environ['API_TIMEOUT'] = '45'
        try:
            from worker.arcworker import config as worker_config
            importlib.reload(worker_config)
            self.assertEqual(worker_config.API_TIMEOUT, 45)
        finally:
            if old is None:
                os.environ.pop('API_TIMEOUT', None)
            else:
                os.environ['API_TIMEOUT'] = old
            from worker.arcworker import config as worker_config
            importlib.reload(worker_config)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
