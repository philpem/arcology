#!/usr/bin/env python3
"""
Switch the database schema to match a target git branch.

Finds migration files added on the current branch (vs the target branch),
determines the downgrade target revision, then runs `flask db downgrade`.

Run from the repo root with the virtual environment active:

    python devtools/db_branch_switch.py              # downgrade to master
    python devtools/db_branch_switch.py main         # downgrade to main
    python devtools/db_branch_switch.py --dry-run    # preview only, no changes
    python devtools/db_branch_switch.py -y           # skip confirmation prompt
"""

import argparse
import os
import re
import subprocess
import sys

MIGRATIONS_DIR = 'migrations/versions'


def _git(*args):
    result = subprocess.run(['git'] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def _find_new_migrations(target_branch):
    """Return migration file paths added on current branch vs target."""
    output = _git(
        'diff', f'{target_branch}...HEAD',
        '--name-only', '--diff-filter=A',
        '--', f'{MIGRATIONS_DIR}',
    )
    if not output:
        return []
    return [f for f in output.splitlines() if f.strip().endswith('.py')]


def _parse_migration(filepath):
    """Return (revision, down_revision) strings from a migration file."""
    with open(filepath) as fh:
        content = fh.read()
    rev = re.search(r'^revision\s*=\s*[\'"]([^\'"]+)[\'"]', content, re.MULTILINE)
    down = re.search(r'^down_revision\s*=\s*(?:[\'"]([^\'"]+)[\'"]|(None))', content, re.MULTILINE)
    revision = rev.group(1) if rev else None
    down_revision = down.group(1) if (down and down.group(1)) else None
    return revision, down_revision


def _find_downgrade_target(migration_files):
    """
    Return the revision to downgrade TO (i.e. the target branch head).

    The new migrations on this branch form a chain.  The root of that chain
    is the one whose down_revision is not itself a new migration.  That
    down_revision is where the target branch's head was -- so it's where we
    need to land.  Returns None if the entire history is new (downgrade to base).
    """
    revisions = {}
    for filepath in migration_files:
        rev, down_rev = _parse_migration(filepath)
        if rev:
            revisions[rev] = down_rev

    all_new = set(revisions)
    for rev, down_rev in revisions.items():
        if down_rev not in all_new:
            return down_rev  # None means "base"

    # Cycle or parse error -- shouldn't happen in a healthy migration chain
    print("Warning: could not determine chain root; defaulting to 'base'.", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'target_branch', nargs='?', default='master',
        help='Branch to match DB schema to (default: master)',
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='Skip confirmation prompt')
    args = parser.parse_args()

    if not os.path.isdir(MIGRATIONS_DIR):
        print(f"Error: {MIGRATIONS_DIR!r} not found. Run from the repo root.", file=sys.stderr)
        sys.exit(1)

    probe = subprocess.run(
        ['git', 'rev-parse', '--verify', args.target_branch],
        capture_output=True,
    )
    if probe.returncode != 0:
        print(f"Error: branch '{args.target_branch}' not found.", file=sys.stderr)
        sys.exit(1)

    current_branch = _git('rev-parse', '--abbrev-ref', 'HEAD')
    print(f"Current branch : {current_branch}")
    print(f"Target branch  : {args.target_branch}")

    new_migrations = _find_new_migrations(args.target_branch)
    if not new_migrations:
        print("\nNo new migrations on this branch vs target. Nothing to do.")
        sys.exit(0)

    print(f"\nMigrations to undo ({len(new_migrations)}):")
    for f in sorted(new_migrations):
        print(f"  {f}")

    target_rev = _find_downgrade_target(new_migrations)
    target_arg = target_rev if target_rev else 'base'
    print(f"\nDowngrade target : {target_arg}")
    print(f"Command          : flask db downgrade {target_arg}")

    if args.dry_run:
        print("\n[dry-run] No changes made.")
        sys.exit(0)

    if not args.yes:
        try:
            answer = input("\nProceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if answer != 'y':
            print("Aborted.")
            sys.exit(1)

    result = subprocess.run(['flask', 'db', 'downgrade', target_arg])
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
