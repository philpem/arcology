# Switching Branches with Database Migrations

## The Problem

The PostgreSQL schema is managed by Alembic (Flask-Migrate) and is **independent of your git branch**. When you switch branches, the migration files in `migrations/versions/` change, but the live database does not. If your feature branch added or removed migrations, the schema will be out of sync with the code after the switch, causing runtime errors.

The fix is to downgrade the database before switching, then upgrade on the new branch if needed.

## Quick Reference

```
python devtools/db_branch_switch.py              # print downgrade command for master
python devtools/db_branch_switch.py other-branch # print downgrade command for another branch
```

The script prints the exact `flask db downgrade` command to run — both the
local venv form and the Docker form — but does not run it, so you can copy it
to whichever environment the database lives in.

## Full Workflow

### 1. Find the downgrade command

From the repo root:

```bash
python devtools/db_branch_switch.py [target-branch]
```

The script:
1. Finds migration files that exist on your current branch but not on the target branch
2. Traces the chain to find the revision that the target branch is at
3. Prints the command to run (local and Docker variants)

### 2. Run the downgrade command

Copy the printed command and run it in the right environment:

```bash
# Local venv
flask db downgrade <revision>

# Docker
docker compose exec -it web flask db downgrade <revision>
```

### 3. Switch branches

```bash
git checkout master       # or target branch
```

### 4. Upgrade if needed

If the target branch also has migrations ahead of the common ancestor (uncommon when switching to master, typical when switching between two feature branches):

```bash
# Local
flask db upgrade

# Docker
docker compose exec -it web flask db upgrade
```

## Manual Approach (without the script)

If you prefer to do it by hand:

```bash
# 1. Find what revision master's head is
#    Look at down_revision in the earliest migration your branch added:
grep down_revision migrations/versions/<your_new_migration>.py

# 2. Downgrade to that revision
flask db downgrade <revision>

# 3. Switch branches
git checkout master

# 4. Upgrade if needed
flask db upgrade
```

Useful diagnostic commands:

```bash
flask db current          # show what revision the DB is currently at
flask db history          # show the full migration chain
flask db heads            # show all chain heads (should be exactly one)
```

## Why Not Just Switch Branches First?

If you switch branches first:
- The migration files on the new branch no longer include the ones your feature added
- Alembic no longer knows how to downgrade through your feature's migrations (the files are gone)
- You are stuck and must check out the original branch just to run the downgrade

Always downgrade **before** switching.

## Edge Cases

**No new migrations on your branch** — The script exits early with "Nothing to do." You can switch branches freely.

**Your branch's migrations form a multi-step chain** — The script handles this automatically; it follows the chain to find the root and downgrades past all of them in one `flask db downgrade` call (Alembic walks the chain itself).

**Switching between two feature branches** (neither is master) — Pass the target branch name explicitly:

```bash
python devtools/db_branch_switch.py other-feature-branch
git checkout other-feature-branch
flask db upgrade
```

**Downgrade to the very beginning** — If every migration is new (unusual), the target is `base` and the script will pass that to Alembic, which removes the entire schema. This is almost never what you want; double-check with `--dry-run` first.
