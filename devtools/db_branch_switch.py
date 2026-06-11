#!/usr/bin/env python3
"""
Print the flask db downgrade command needed before switching to a target branch.

Finds migration files added on the current branch (vs the target branch) and
prints the exact command to run -- both for a local venv and for Docker.  It
does NOT run the command itself, so you can copy it to whichever environment
the database lives in.

Run from the repo root:

    python devtools/db_branch_switch.py              # compare against master
    python devtools/db_branch_switch.py other-branch # compare against a different branch
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
    for _rev, down_rev in revisions.items():
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

    print(f"\nDowngrade target: {target_arg}")

    if target_arg == 'base':
        # 'base' means the new-on-this-branch chain starts from scratch
        # (down_revision=None).  This most commonly occurs when switching
        # across a migration-squash boundary: the squash replaced many
        # individual revisions with a single consolidated migration whose
        # down_revision is None.  If your DB is already stamped at a
        # revision that exists on the target branch, you may NOT need to
        # downgrade at all.
        print()
        print("WARNING: downgrading to 'base' will DROP the entire schema.")
        print("  Check your current revision before running this command:")
        print("    flask db current")
        print("  If the reported revision ID appears in the target branch's")
        print("  migration chain, no downgrade is needed — just switch branches")
        print("  and run 'flask db upgrade' as usual.")
        print()

    print("\nRun ONE of the following before switching branches:")
    print("\n  Local venv:")
    print(f"    flask db downgrade {target_arg}")
    print("\n  Docker:")
    print(f"    docker compose exec -it web flask db downgrade {target_arg}")


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
