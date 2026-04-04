# Migration Conflict Detection

## The Problem

When two pull requests independently add an Alembic migration, both will set `down_revision` to the current chain head. Each PR passes CI on its own. After both merge, the migration directory contains two files pointing at the same parent, creating a branch:

```
           ┌─ migration_A (from PR #1)
head ──────┤
           └─ migration_B (from PR #2)   ← "Multiple head revisions"
```

`flask db upgrade` then fails with:

> ERROR: Multiple head revisions are present for given argument 'head'

## How We Detect It

### Layer 1: CI (within-branch)

`ci/check_migration_sanity.py` runs on every push and PR. It verifies the migration chain is linear with no duplicate `down_revision` values. This catches conflicts that already exist in the branch (e.g. after a merge).

### Layer 2: CI (cross-branch, PRs only)

`ci/check_migration_conflict.py` runs on pull requests. It compares new migrations in the PR against the target branch and flags when a new migration's `down_revision` already has a child on the target branch. This catches the two-PR scenario **before** merge.

### Layer 3: Local pre-push hook

The `hooks/pre-push` hook runs both checks before pushing. Install it with:

```bash
git config core.hooksPath hooks
```

To bypass in an emergency: `git push --no-verify`

## Branch Protection (Recommended)

The most robust prevention is enabling **"Require branches to be up to date before merging"** in GitHub branch protection rules:

1. Go to **Settings > Branches > Branch protection rules** for `master`
2. Enable **"Require status checks to pass before merging"**
3. Check **"Require branches to be up to date before merging"**

This forces the second PR to rebase onto master after the first PR merges. The rebase surfaces the conflict in the existing sanity check, and the developer must update their migration's `down_revision`.

**Trade-off:** This serialises merges -- every PR must be current with master. For small teams this is fine. For larger teams, consider GitHub's [merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) feature.

## Resolving a Conflict

If a migration branch conflict has already landed:

### Option A: Edit down_revision (preferred)

Identify which migration should come second, and update its `down_revision` to point to the other migration's `revision`:

```python
# Before (conflict -- both point to the same parent):
# migration_A.py:  down_revision = 'abc123'
# migration_B.py:  down_revision = 'abc123'

# After (linear chain):
# migration_A.py:  down_revision = 'abc123'
# migration_B.py:  down_revision = '<revision of migration_A>'
```

### Option B: Alembic merge

Create a merge migration that combines both heads:

```bash
flask db merge heads -m "Merge migration heads"
```

This creates a new migration with two parents. It works but adds a merge node to the chain. Option A is cleaner.

After resolving, verify with:

```bash
flask db heads    # Should show exactly one head
python ci/check_migration_sanity.py   # Should pass
```
