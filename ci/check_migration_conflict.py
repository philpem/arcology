#!/usr/bin/env python3
"""Cross-branch migration conflict detection for pull requests.

Detects when a PR adds a migration whose down_revision already has a child
on the target branch (e.g. master).  This catches the "multiple heads" problem
that occurs when two PRs independently extend the same migration chain head.

Designed for use in CI (GitHub Actions) on pull_request events.  Requires
git and a fetched target ref.  Uses only stdlib.

Usage:
    python ci/check_migration_conflict.py --target-ref origin/master
"""

import argparse
import ast
import os
import subprocess
import sys


MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'migrations', 'versions'
)


def parse_migration_source(source, filename):
    """Parse migration source code and extract revision/down_revision."""
    tree = ast.parse(source, filename)
    info = {'filename': filename, 'revision': None, 'down_revision': None}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == 'revision' and isinstance(node.value, ast.Constant):
                        info['revision'] = node.value.value
                    elif target.id == 'down_revision' and isinstance(node.value, ast.Constant):
                        info['down_revision'] = node.value.value
    return info


def git_ls_tree(ref, path):
    """List filenames at path on the given git ref."""
    result = subprocess.run(
        ['git', 'ls-tree', '--name-only', ref, path + '/'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    # ls-tree returns full paths like "migrations/versions/foo.py"
    return [os.path.basename(line) for line in result.stdout.strip().splitlines() if line]


def git_show(ref, filepath):
    """Get file contents from a git ref."""
    result = subprocess.run(
        ['git', 'show', f'{ref}:{filepath}'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout


def get_target_migrations(target_ref):
    """Parse all migrations from the target branch."""
    target_files = git_ls_tree(target_ref, 'migrations/versions')
    if target_files is None:
        return None

    migrations = []
    for filename in target_files:
        if not filename.endswith('.py'):
            continue
        source = git_show(target_ref, f'migrations/versions/{filename}')
        if source is None:
            continue
        try:
            migrations.append(parse_migration_source(source, filename))
        except SyntaxError:
            pass  # Skip unparseable files
    return migrations


def get_local_migrations():
    """Parse all migrations from the working tree."""
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    migrations = []
    for filename in sorted(os.listdir(MIGRATIONS_DIR)):
        if not filename.endswith('.py'):
            continue
        filepath = os.path.join(MIGRATIONS_DIR, filename)
        with open(filepath, 'r') as f:
            source = f.read()
        try:
            migrations.append(parse_migration_source(source, filename))
        except SyntaxError:
            pass
    return migrations


def get_new_migrations(local_migrations, target_migrations):
    """Return migrations introduced locally that do not already exist on target.

    We key this by Alembic revision, not filename, so pure renames or file moves
    of an existing migration on the target branch are not treated as new work.
    """
    target_filenames = {m['filename'] for m in target_migrations}
    target_revisions = {m['revision'] for m in target_migrations if m['revision']}
    return [
        m for m in local_migrations
        if m['filename'] not in target_filenames
        and m['revision'] not in target_revisions
    ]


def find_conflicts(new_migrations, target_migrations, target_ref):
    """Return conflict messages for new migrations that would create heads."""
    target_children = {}  # down_revision -> migration that depends on it
    for m in target_migrations:
        if m['down_revision'] is not None:
            target_children[m['down_revision']] = m

    new_revisions = {m['revision'] for m in new_migrations if m['revision']}

    errors = []
    for m in new_migrations:
        down = m['down_revision']
        if down is None:
            continue

        if down in new_revisions:
            continue

        if down in target_children:
            existing = target_children[down]
            errors.append(
                f"  CONFLICT: {m['filename']} has down_revision='{down}',\n"
                f"    but {existing['filename']} on {target_ref} also "
                f"extends revision '{down}'.\n"
                f"    This will create multiple Alembic heads after merge.\n"
                f"    Fix: rebase onto {target_ref} and update your "
                f"migration's down_revision to point to the current chain head."
            )

    return errors


def main():
    parser = argparse.ArgumentParser(
        description='Detect cross-branch migration conflicts.')
    parser.add_argument(
        '--target-ref', default='origin/master',
        help='Git ref for the target branch (default: origin/master)')
    args = parser.parse_args()

    # Get target branch migrations
    target_migrations = get_target_migrations(args.target_ref)
    if target_migrations is None:
        print(f"SKIP: Could not read migrations from {args.target_ref} "
              f"(ref not available).")
        return 0

    # Get local migrations
    local_migrations = get_local_migrations()
    if not local_migrations:
        print("SKIP: No local migration files found.")
        return 0

    # Find files that are new in this branch (not on target)
    new_migrations = get_new_migrations(local_migrations, target_migrations)

    if not new_migrations:
        print("OK: No new migrations in this branch.")
        return 0

    print(f"Checking {len(new_migrations)} new migration(s) against "
          f"{args.target_ref}...\n")

    errors = find_conflicts(new_migrations, target_migrations, args.target_ref)

    if errors:
        print("ERRORS:")
        for e in errors:
            print(e)
        print()
        print("FAIL: Cross-branch migration conflict detected.")
        print("      Rebase your branch onto the target branch to resolve.")
        return 1

    print("OK: No cross-branch migration conflicts detected.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
