#!/usr/bin/env python3
"""Run the application test suite with SQLAlchemy warnings escalated to errors.

A bare ``python -W error::sqlalchemy.exc.SAWarning -m unittest ...`` does *not*
work: the interpreter resolves the ``-W`` warning category during startup,
before third-party packages are importable, so it silently discards the option
with ``Invalid -W option ignored: invalid module name: 'sqlalchemy.exc'``.

Importing SQLAlchemy first and installing the filter programmatically is
reliable.  This escalates ``SAWarning`` (e.g. the cartesian-product-join notice)
to an exception so such a query fails the build instead of slipping through
unnoticed.  See ``myapp/utils/warnings.py`` for the runtime (logging + Sentry)
side, and ``ci/test_cartesian_join.py`` for an explicit guard.

Run locally exactly as CI does:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python ci/run_app_tests.py
"""

import os
import sys
import unittest
import warnings
from sqlalchemy.exc import SAWarning

_CI_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_CI_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Escalate SQLAlchemy warnings to errors so genuine query problems (e.g.
# cartesian-product joins) fail the build.  Broad on purpose: any SAWarning is
# worth a look.  Prepended (default) so it takes precedence over the
# lower-priority 'always' filter that
# myapp.utils.warnings.install_warning_capture() appends at app startup.
warnings.filterwarnings('error', category=SAWarning)

# ...except the SQLite-only "Can't sort tables for DROP" cycle warning emitted
# during test teardown (drop_all).  The analyses<->artefacts FK cycle is known,
# and SQLite -- unlike the production PostgreSQL backend, which supports ALTER --
# cannot order the DROP.  It is a test-harness artifact, not a query bug.
# Registered last so it is checked first and wins for this specific message.
warnings.filterwarnings('ignore', category=SAWarning,
                        message=r"Can't sort tables for DROP")


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=_CI_DIR, pattern='test_*.py')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
