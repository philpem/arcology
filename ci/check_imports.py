#!/usr/bin/env python3
"""Verify that key modules can be imported without errors.

This catches broken imports after refactoring (e.g. the shared module
consolidation). Requires project dependencies to be installed first.

Exits 0 if all imports succeed, 1 if any fail.
"""

import importlib
import os
import sys

# Modules to import — covers the main app, shared definitions, and worker
MODULES_TO_CHECK = [
    'arcology_shared.enums',
    'arcology_shared.archive_formats',
    'myapp.extensions',
    'myapp.database',
    'myapp.app',
]


def main():
    # Ensure repo root is on sys.path (same as worker/worker.py does)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    errors = []

    for module_name in MODULES_TO_CHECK:
        try:
            importlib.import_module(module_name)
            print(f"  OK: {module_name}")
        except Exception as e:
            errors.append((module_name, e))
            print(f"  FAIL: {module_name} — {type(e).__name__}: {e}")

    print()
    if errors:
        print(f"FAIL: {len(errors)} import(s) failed.")
        return 1

    print("OK: All key modules imported successfully.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
