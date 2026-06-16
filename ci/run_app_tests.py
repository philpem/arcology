#!/usr/bin/env python3
"""Run the application test suite with warnings escalated to errors.

A bare ``python -W error::sqlalchemy.exc.SAWarning -m unittest ...`` does *not*
work for the SQLAlchemy categories: the interpreter resolves the ``-W`` warning
category during startup, before third-party packages are importable, so it
silently discards the option with
``Invalid -W option ignored: invalid module name: 'sqlalchemy.exc'``.

Importing the categories first and installing the filters programmatically is
reliable.  This escalates the deterministic, high-signal warning categories to
exceptions so they fail the build instead of slipping through unnoticed:

* ``SAWarning`` — e.g. the cartesian-product-join notice.
* ``DeprecationWarning`` / ``PendingDeprecationWarning`` — including SQLAlchemy's
  ``LegacyAPIWarning`` (``Query.get()`` etc.); catches API rot before a
  dependency upgrade removes the deprecated call.
* ``SyntaxWarning`` / ``RuntimeWarning`` / ``FutureWarning`` — invalid escape
  sequences, "coroutine never awaited", library behaviour-change notices.

``ResourceWarning`` is deliberately *not* escalated: unclosed-file warnings are
garbage-collector-timing dependent and would make CI flaky.  They remain visible
in the runtime logs (``myapp/utils/warnings.py`` enables ``captureWarnings``).

See ``myapp/utils/warnings.py`` for the runtime (logging + Sentry) side, and
``ci/test_cartesian_join.py`` for an explicit cartesian-join guard.

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

# Escalate the deterministic, high-signal warning categories to errors so a
# genuine problem fails the build.  Prepended (default) so these take precedence
# over the lower-priority 'always' filter that
# myapp.utils.warnings.install_warning_capture() appends at app startup.
_ESCALATE = (
    SAWarning,
    DeprecationWarning,       # includes SQLAlchemy LegacyAPIWarning (Query.get)
    PendingDeprecationWarning,
    SyntaxWarning,
    RuntimeWarning,
    FutureWarning,
)
for _category in _ESCALATE:
    warnings.filterwarnings('error', category=_category)

# ...except the SQLite-only "Can't sort tables for DROP" cycle warning emitted
# during test teardown (drop_all).  The analyses<->artefacts FK cycle is known,
# and SQLite -- unlike the production PostgreSQL backend, which supports ALTER --
# cannot order the DROP.  It is a test-harness artifact, not a query bug.
# Registered after the escalations so it is checked first and wins for this
# specific message.
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
