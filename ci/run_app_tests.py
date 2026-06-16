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

Speed: the suite is split across worker processes (one test module per task,
so each module's tests still run serially within a single interpreter — only
*different* modules run concurrently).  Defaults to ``os.cpu_count()`` workers;
override with ``--jobs N`` or the ``TEST_JOBS`` environment variable.  Use
``--jobs 1`` for the old single-process behaviour (e.g. when debugging
cross-test state).

Run locally exactly as CI does:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python ci/run_app_tests.py
"""

import argparse
import glob
import io
import os
import sys
import unittest
import warnings
from concurrent.futures import ProcessPoolExecutor
from sqlalchemy.exc import SAWarning

_CI_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_CI_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _install_warning_filters():
    """Escalate the deterministic, high-signal warning categories to errors.

    Called at import (so it applies in every worker process, whether spawned or
    forked) and defensively at the start of each worker run.
    """
    # Prepended (default) so these take precedence over the lower-priority
    # 'always' filter that myapp.utils.warnings.install_warning_capture()
    # appends at app startup.
    escalate = (
        SAWarning,
        DeprecationWarning,       # includes SQLAlchemy LegacyAPIWarning (Query.get)
        PendingDeprecationWarning,
        SyntaxWarning,
        RuntimeWarning,
        FutureWarning,
    )
    for category in escalate:
        warnings.filterwarnings('error', category=category)

    # ...except the SQLite-only "Can't sort tables for DROP" cycle warning
    # emitted during test teardown (drop_all).  The analyses<->artefacts FK
    # cycle is known, and SQLite -- unlike the production PostgreSQL backend,
    # which supports ALTER -- cannot order the DROP.  It is a test-harness
    # artifact, not a query bug.  Registered after the escalations so it is
    # checked first and wins for this specific message.
    warnings.filterwarnings('ignore', category=SAWarning,
                            message=r"Can't sort tables for DROP")


_install_warning_filters()


def _discover_module_names():
    """Return the importable names of every ``ci/test_*.py`` module."""
    names = []
    for path in sorted(glob.glob(os.path.join(_CI_DIR, 'test_*.py'))):
        stem = os.path.splitext(os.path.basename(path))[0]
        names.append(f'ci.{stem}')
    return names


def _run_module(module_name):
    """Run a single test module in this (worker) process.

    Returns a result dict that the parent aggregates.  Output is captured so
    concurrent workers don't interleave on the console.
    """
    _install_warning_filters()
    stream = io.StringIO()
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromName(module_name)
    except Exception as exc:  # import-time error: surface it as a failure
        return {
            'module': module_name,
            'testsRun': 0,
            'failures': 1,
            'errors': 0,
            'skipped': 0,
            'success': False,
            'output': f'ERROR: could not load {module_name}: {exc!r}\n',
        }
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    return {
        'module': module_name,
        'testsRun': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'success': result.wasSuccessful(),
        'output': stream.getvalue(),
    }


def _run_serial(module_names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--jobs', '-j', type=int,
        default=int(os.environ.get('TEST_JOBS', '0')) or (os.cpu_count() or 1),
        help='Number of worker processes (default: TEST_JOBS env or CPU count). '
             'Use 1 for single-process execution.',
    )
    args = parser.parse_args()

    module_names = _discover_module_names()
    if not module_names:
        print('No test modules found.')
        return 1

    jobs = max(1, min(args.jobs, len(module_names)))
    if jobs == 1:
        return _run_serial(module_names)

    results = []
    with ProcessPoolExecutor(max_workers=jobs) as executor:
        for result in executor.map(_run_module, module_names):
            results.append(result)
            # Stream each module's output as it completes so a hang is visible.
            sys.stdout.write(result['output'])
            sys.stdout.flush()

    total_run = sum(r['testsRun'] for r in results)
    total_failures = sum(r['failures'] for r in results)
    total_errors = sum(r['errors'] for r in results)
    total_skipped = sum(r['skipped'] for r in results)
    failed_modules = [r['module'] for r in results if not r['success']]

    print('\n' + '=' * 70)
    print(f'Ran {total_run} tests across {len(module_names)} modules '
          f'using {jobs} workers')
    print(f'  failures={total_failures} errors={total_errors} '
          f'skipped={total_skipped}')
    if failed_modules:
        print('\nFAILED modules:')
        for name in failed_modules:
            print(f'  - {name}')
        print('=' * 70)
        return 1
    print('OK')
    print('=' * 70)
    return 0


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
