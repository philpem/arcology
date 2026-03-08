#!/usr/bin/env python3
"""Check all Python files for syntax errors.

Uses only stdlib — no dependencies needed. Exits 0 if all files are valid,
1 if any syntax errors are found.
"""

import os
import py_compile
import sys


# Directories to check, relative to the repository root
CHECK_DIRS = ['myapp', 'worker', 'shared', 'ci', 'migrations']


def find_python_files(base_dir):
    """Yield all .py files under base_dir."""
    for root, _dirs, files in os.walk(base_dir):
        for filename in sorted(files):
            if filename.endswith('.py'):
                yield os.path.join(root, filename)


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    errors = []

    for check_dir in CHECK_DIRS:
        full_path = os.path.join(repo_root, check_dir)
        if not os.path.isdir(full_path):
            continue
        for filepath in find_python_files(full_path):
            try:
                py_compile.compile(filepath, doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(str(e))

    if errors:
        print(f"FAIL: {len(errors)} syntax error(s) found:\n")
        for error in errors:
            print(f"  {error}")
        return 1

    print("OK: All Python files have valid syntax.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
