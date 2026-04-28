#!/usr/bin/env python3
"""Static analysis of Alembic migration files to catch known pitfalls.

Checks:
1. Autocommit: Migrations using ALTER TYPE ADD VALUE or RENAME VALUE must
   set autocommit = True at module level (PostgreSQL cannot run these inside
   a transaction, and env.py uses transaction_per_migration=True).

2. Chain integrity: The down_revision chain must be linear and unbroken
   from the initial migration (down_revision=None) to head.

3. Downgrade body: Warns (non-fatal) if a migration has an empty downgrade()
   function — acceptable for enum additions but worth flagging.

Uses only stdlib. Exits 0 if all checks pass, 1 on errors, 2 on warnings only.
"""

import ast
import os
import re
import sys

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'migrations', 'versions'
)

# Patterns that require autocommit = True
NEEDS_AUTOCOMMIT_PATTERNS = [
    re.compile(r'ALTER\s+TYPE\s+\w+\s+ADD\s+VALUE', re.IGNORECASE),
    re.compile(r'ALTER\s+TYPE\s+\w+\s+RENAME\s+VALUE', re.IGNORECASE),
]

HEADER_REVISION_RE = re.compile(
    r'^[ \t]*Revision ID:[ \t]*([^\r\n]*)[ \t]*$', re.MULTILINE
)
HEADER_DOWN_REVISION_RE = re.compile(
    r'^[ \t]*Revises:[ \t]*([^\r\n]*)[ \t]*$', re.MULTILINE
)


def _normalise_header_revision(value):
    """Normalise header values so blank/none-like strings compare cleanly."""
    if value is None:
        return None
    value = value.strip()
    if value.lower() in {'', 'none', 'null'}:
        return None
    return value


def parse_header_metadata(source):
    """Extract Revision ID / Revises values from the migration header comments."""
    revision_match = HEADER_REVISION_RE.search(source)
    down_revision_match = HEADER_DOWN_REVISION_RE.search(source)
    return {
        'header_revision': _normalise_header_revision(
            revision_match.group(1) if revision_match else None
        ),
        'header_down_revision': _normalise_header_revision(
            down_revision_match.group(1) if down_revision_match else None
        ),
    }


def parse_migration(filepath):
    """Parse a migration file and extract key attributes."""
    with open(filepath, 'r') as f:
        source = f.read()

    tree = ast.parse(source, filepath)
    info = {
        'filepath': filepath,
        'filename': os.path.basename(filepath),
        'source': source,
        'revision': None,
        'down_revision': None,
        'header_revision': None,
        'header_down_revision': None,
        'has_autocommit': False,
        'autocommit_value': None,
        'has_empty_downgrade': False,
    }
    info.update(parse_header_metadata(source))

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == 'revision' and isinstance(node.value, ast.Constant):
                        info['revision'] = node.value.value
                    elif target.id == 'down_revision':
                        if isinstance(node.value, ast.Constant):
                            info['down_revision'] = node.value.value
                    elif target.id == 'autocommit':
                        info['has_autocommit'] = True
                        if isinstance(node.value, ast.Constant):
                            info['autocommit_value'] = node.value.value

        elif isinstance(node, ast.FunctionDef) and node.name == 'downgrade':
            # Check if body is just 'pass' or empty
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                info['has_empty_downgrade'] = True
            elif len(body) == 1 and isinstance(body[0], ast.Expr):
                if isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    # Body is just a docstring
                    info['has_empty_downgrade'] = True

    return info


def format_migration(migration):
    """Return a compact label for a migration."""
    return f"{migration['filename']} (revision={migration['revision']})"


def check_autocommit(migrations):
    """Check that migrations using ALTER TYPE have autocommit = True."""
    errors = []
    for m in migrations:
        needs_autocommit = any(
            p.search(m['source']) for p in NEEDS_AUTOCOMMIT_PATTERNS
        )
        if needs_autocommit and not m['has_autocommit']:
            errors.append(
                f"  {m['filename']}: Contains ALTER TYPE ADD/RENAME VALUE "
                f"but is missing 'autocommit = True' at module level.\n"
                f"    PostgreSQL cannot run ALTER TYPE ADD/RENAME VALUE "
                f"inside a transaction.\n"
                f"    Add 'autocommit = True' after depends_on."
            )
        elif needs_autocommit and m['autocommit_value'] is not True:
            errors.append(
                f"  {m['filename']}: Has 'autocommit' but it is not set "
                f"to True (found: {m['autocommit_value']!r})."
            )
    return errors


def check_chain_integrity(migrations):
    """Verify the revision chain is linear and unbroken."""
    errors = []

    if not migrations:
        return errors

    # Build maps
    by_revision = {}
    by_down_revision = {}
    roots = []

    for m in migrations:
        rev = m['revision']
        down = m['down_revision']

        if rev is None:
            errors.append(f"  {m['filename']}: Missing 'revision' identifier.")
            continue

        if rev in by_revision:
            errors.append(
                f"  Duplicate revision '{rev}' in {m['filename']} and "
                f"{by_revision[rev]['filename']}."
            )
            continue

        by_revision[rev] = m

        if down is None:
            roots.append(m)
        else:
            by_down_revision.setdefault(down, []).append(m)

    if len(roots) == 0:
        errors.append("  No root migration found (no migration with down_revision=None).")
    elif len(roots) > 1:
        names = ', '.join(format_migration(m) for m in roots)
        errors.append(f"  Multiple root migrations found: {names}")

    branch_points = {
        down: children for down, children in by_down_revision.items()
        if len(children) > 1
    }
    if branch_points:
        errors.append("  Multiple children detected for the same down_revision:")
        for down, children in sorted(branch_points.items()):
            child_list = ', '.join(format_migration(m) for m in children)
            errors.append(f"    {down} -> {child_list}")

    # Walk the chain from root to head
    if len(roots) == 1 and not errors:
        visited = set()
        current = roots[0]
        while current:
            visited.add(current['revision'])
            next_migrations = by_down_revision.get(current['revision'], [])
            current = next_migrations[0] if next_migrations else None

        unvisited = set(by_revision.keys()) - visited
        if unvisited:
            names = ', '.join(
                format_migration(by_revision[rev]) for rev in sorted(unvisited)
            )
            errors.append(f"  Orphaned migrations not reachable from root: {names}")

    all_children = {
        child['revision']
        for children in by_down_revision.values()
        for child in children
    }
    heads = [
        m for rev, m in sorted(by_revision.items())
        if rev not in all_children
    ]
    if len(heads) == 0:
        errors.append("  No head migration found.")
    elif len(heads) > 1:
        head_list = ', '.join(format_migration(m) for m in heads)
        errors.append(f"  Multiple heads found: {head_list}")

    return errors


def check_filename_order_matches_chain(migrations):
    """Verify lexicographic filename order matches the linear migration chain."""
    errors = []

    if not migrations:
        return errors

    by_revision = {}
    by_down_revision = {}
    roots = []

    for m in migrations:
        rev = m['revision']
        down = m['down_revision']

        if rev is None:
            return errors

        if rev in by_revision:
            return errors

        by_revision[rev] = m

        if down is None:
            roots.append(m)
        else:
            by_down_revision.setdefault(down, []).append(m)

    if len(roots) != 1:
        return errors

    branch_points = [
        down for down, children in by_down_revision.items()
        if len(children) > 1
    ]
    if branch_points:
        return errors

    chain = []
    current = roots[0]
    seen = set()
    while current and current['revision'] not in seen:
        chain.append(current)
        seen.add(current['revision'])
        next_migrations = by_down_revision.get(current['revision'], [])
        current = next_migrations[0] if next_migrations else None

    if len(chain) != len(by_revision):
        return errors

    filename_order = [m['filename'] for m in sorted(migrations, key=lambda m: m['filename'])]
    chain_order = [m['filename'] for m in chain]

    if filename_order != chain_order:
        errors.append(
            "  Lexicographic migration filename order does not match the down_revision chain."
        )
        errors.append(f"    File listing order: {' -> '.join(filename_order)}")
        errors.append(f"    Chain order:        {' -> '.join(chain_order)}")

    return errors


def check_downgrade_bodies(migrations):
    """Warn about empty downgrade functions."""
    warnings = []
    for m in migrations:
        if m['has_empty_downgrade']:
            warnings.append(
                f"  {m['filename']}: downgrade() is empty (pass only). "
                f"This migration cannot be rolled back."
            )
    return warnings


def check_header_consistency(migrations):
    """Warn when header comments disagree with module revision identifiers."""
    warnings = []
    for m in migrations:
        if (
            m['header_revision'] is not None
            and m['header_revision'] != m['revision']
        ):
            warnings.append(
                f"  {m['filename']}: Header 'Revision ID: {m['header_revision']}' "
                f"does not match revision={m['revision']!r}."
            )
        if (
            m['header_down_revision'] is not None
            and m['header_down_revision'] != m['down_revision']
        ):
            warnings.append(
                f"  {m['filename']}: Header 'Revises: {m['header_down_revision']}' "
                f"does not match down_revision={m['down_revision']!r}."
            )
    return warnings


def main():
    if not os.path.isdir(MIGRATIONS_DIR):
        print(f"SKIP: Migrations directory not found: {MIGRATIONS_DIR}")
        return 0

    migration_files = sorted(
        f for f in os.listdir(MIGRATIONS_DIR) if f.endswith('.py')
    )

    if not migration_files:
        print("SKIP: No migration files found.")
        return 0

    print(f"Checking {len(migration_files)} migration(s)...\n")

    migrations = []
    parse_errors = []
    for filename in migration_files:
        filepath = os.path.join(MIGRATIONS_DIR, filename)
        try:
            migrations.append(parse_migration(filepath))
        except SyntaxError as e:
            parse_errors.append(f"  {filename}: SyntaxError: {e}")

    all_errors = []
    all_warnings = []

    if parse_errors:
        all_errors.extend(parse_errors)

    # Run checks
    autocommit_errors = check_autocommit(migrations)
    if autocommit_errors:
        all_errors.append("Autocommit check failed:")
        all_errors.extend(autocommit_errors)

    chain_errors = check_chain_integrity(migrations)
    if chain_errors:
        all_errors.append("Chain integrity check failed:")
        all_errors.extend(chain_errors)

    order_warnings = check_filename_order_matches_chain(migrations)
    if order_warnings:
        all_warnings.append("Filename ordering warnings:")
        all_warnings.extend(order_warnings)

    downgrade_warnings = check_downgrade_bodies(migrations)
    if downgrade_warnings:
        all_warnings.append("Downgrade warnings:")
        all_warnings.extend(downgrade_warnings)

    header_warnings = check_header_consistency(migrations)
    if header_warnings:
        all_warnings.append("Header consistency warnings:")
        all_warnings.extend(header_warnings)

    # Report results
    if all_warnings:
        print("WARNINGS:")
        for w in all_warnings:
            print(f"  {w}")
        print()

    if all_errors:
        print("ERRORS:")
        for e in all_errors:
            print(f"  {e}")
        print()
        print("FAIL: Migration sanity checks found errors.")
        return 1

    if all_warnings:
        print("OK: No errors (warnings above are informational).")
        return 0

    print("OK: All migration sanity checks passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

# vim: ts=4 sw=4 et
