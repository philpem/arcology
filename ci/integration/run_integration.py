#!/usr/bin/env python3
"""Entry point for the worker analysis-pipeline integration suite.

Sets up ``sys.path`` so both the worker package (``arcworker``) and the repo
root (``arcology_shared``) are importable, then discovers and runs the
``it_*.py`` modules in this directory.  These are deliberately NOT named
``test_*`` so the app-tests job's ``discover -p 'test_*.py'`` never picks them
up — they need the worker container's external tools.

Usage:
    python3 ci/integration/run_integration.py [-v] [--regen]

``--regen`` (or ``ARCOLOGY_IT_REGEN=1``) rewrites the golden files instead of
asserting against them.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
WORKER_DIR = os.path.join(REPO_ROOT, 'worker')

for path in (HERE, WORKER_DIR, REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--regen' in argv:
        argv.remove('--regen')
        os.environ['ARCOLOGY_IT_REGEN'] = '1'

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern='it_*.py', top_level_dir=HERE)

    verbosity = 2 if ('-v' in argv or '--verbose' in argv) else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
