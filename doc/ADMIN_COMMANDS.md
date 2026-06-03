# Admin Commands

Flask CLI commands for managing the Arcology instance. In Docker, prefix
each command with `docker compose exec web`.

## set-password

Forcibly set the local-authentication password for an existing user. Use this
to recover a locked-out account without needing shell database access.

```bash
flask set-password alice                         # interactive prompt
flask set-password alice --password 'newpassword123'

# Bypass the 12-character minimum (emergency recovery only):
flask set-password alice --no-min-length --password 'short'
```

The `--no-min-length` flag exists purely for account recovery edge cases where
a short password must be set temporarily. Change it to a strong password
immediately after regaining access.

## create-admin

Create an administrator user account. Idempotent — skips if a user already
exists.

```bash
flask create-admin                    # interactive prompt
flask create-admin --username admin --password 'mysecurepassword'

# Non-interactive (Docker / CI): set ADMIN_USERNAME and ADMIN_PASSWORD env vars
```

## rebuild-search-index

Rebuild the search index tables (protection, mastering, partition detection)
from completed analysis results. Run after migrations or to fix
inconsistencies.

```bash
flask rebuild-search-index
```

## backfill-slugs

Populate the `slug` column for Items and Artefacts where it is NULL.  This
affects rows created before slug-on-write was added to the creation paths.

Run once after deploying the slug-on-write fix to bring legacy rows up to date.
The command is idempotent — rows that already have a slug are skipped.

```bash
flask backfill-slugs                         # assign slugs to all NULL rows
flask backfill-slugs --dry-run               # preview without changes
flask backfill-slugs --batch-size 200        # smaller commit batches
```

Slugs are assigned using the same `ensure_unique_slug` logic as new rows:
Items use global uniqueness; Artefacts are scoped per item (so two artefacts
labelled "Disc 1" in different items each get `disc-1`, not `disc-1` and
`disc-1-2`).

## rescan-hashes

Re-link extracted files to active hash databases without re-analysing.
Useful after importing new hash databases.

```bash
flask rescan-hashes                          # all artefacts
flask rescan-hashes --artefact <UUID>        # single artefact
flask rescan-hashes --batch-size 1000        # larger batches
```

## reanalyse

Reset and re-queue analysis for artefacts.

**Single-analysis retry** — retry one specific failed analysis without
disturbing other completed work on the same artefact:

```bash
flask reanalyse --analysis ANALYSIS_UUID
flask reanalyse --analysis ANALYSIS_UUID --dry-run
```

This deletes only that analysis and any artefacts it produced, then queues
a fresh job of the same type.  Use `arco debug errors ARTEFACT_UUID` to
find the analysis UUID.

**Full artefact reset** — clears all previous analysis results, derived
artefacts, and output files, then queues fresh analyses based on each
artefact's type.  At least one filter or `--all` is required.

```bash
# Reanalyse everything
flask reanalyse --all
flask reanalyse --all --dry-run              # preview without changes

# Filter by item (accepts URL identifiers or full UUIDs)
flask reanalyse --item ca0dcd66-test
flask reanalyse --item ca0dcd66

# Filter by artefact type
flask reanalyse --artefact-type SCP
flask reanalyse --artefact-type HFE

# Filter by taxonomy
flask reanalyse --platform "Acorn Archimedes"
flask reanalyse --tag "needs-review"
flask reanalyse --category "Games"

# Combine filters (ANDed together)
flask reanalyse --platform "Acorn Archimedes" --artefact-type SCP
flask reanalyse --tag "needs-review" --artefact-type IMG

# Control batch size (default: 50)
flask reanalyse --all --batch-size 100
```

Options:

| Flag | Description |
|------|-------------|
| `--analysis UUID` | Retry a single analysis by UUID (lightweight, preserves other results) |
| `--all` | Select every artefact in the database |
| `--item UUID` | Restrict to artefacts belonging to a single item |
| `--tag NAME` | Restrict to items with this tag |
| `--platform NAME` | Restrict to items on this platform |
| `--category NAME` | Restrict to items in this category |
| `--artefact-type TYPE` | Restrict to this artefact type (e.g. `SCP`, `HFE`, `IMG`) |
| `--dry-run` | Show what would be requeued without making changes |
| `--batch-size N` | Commit every N artefacts (default: 50) |

## cancel-analysis

Delete PENDING analyses without resetting artefact data or re-queuing
anything. Useful for clearing a backlog or removing unwanted jobs queued in
error.

Cancels PENDING analyses by default. Add `--include-running` to also cancel
analyses already claimed by a worker (the worker will finish processing but
its result will be silently discarded).

Exactly one selection method is required.

```bash
# Single analysis by UUID
flask cancel-analysis --analysis ANALYSIS_UUID

# All pending analyses on one artefact
flask cancel-analysis --artefact ARTEFACT_UUID

# All pending analyses matching artefact filters (same flags as reanalyse)
flask cancel-analysis --all
flask cancel-analysis --all --dry-run
flask cancel-analysis --item ITEM_UUID
flask cancel-analysis --platform "Acorn Archimedes"
flask cancel-analysis --tag needs-review --artefact-type HFE

# Also cancel RUNNING analyses
flask cancel-analysis --all --include-running
```

Options:

| Flag | Description |
|------|-------------|
| `--analysis UUID` | Cancel a single analysis by UUID |
| `--artefact UUID` | Cancel all pending analyses on one artefact |
| `--all` | Select every artefact in the database |
| `--item UUID` | Restrict to a single item (URL identifier or full UUID) |
| `--tag NAME` | Restrict to items with this tag |
| `--platform NAME` | Restrict to items on this platform |
| `--category NAME` | Restrict to items in this category |
| `--artefact-type TYPE` | Restrict to this artefact type (e.g. `SCP`, `HFE`) |
| `--include-running` | Also cancel RUNNING analyses |
| `--dry-run` | Show what would be cancelled without making changes |

## reassign-ownership

Bulk-transfers all items and artefacts owned by one user to another. Use
this when a curator leaves and their private collection work needs to be
handed over to a colleague. The source user's account is not modified.

Also accessible via the admin web UI at `/admin/reassign-ownership`.

```bash
# Reassign everything from alice to bob
flask reassign-ownership --from alice --to bob

# Preview without making changes
flask reassign-ownership --from alice --to bob --dry-run

# Release to unowned (no new owner assigned)
flask reassign-ownership --from alice --to none

# Skip the interactive confirmation prompt
flask reassign-ownership --from alice --to bob --yes
```

Options:

| Flag | Description |
|------|-------------|
| `--from USERNAME` | User whose items and artefacts will be transferred (required) |
| `--to USERNAME\|none` | Receiving user, or `none` to leave items unowned (required) |
| `--dry-run` | Show counts without making any changes |
| `--yes` | Skip the interactive confirmation prompt |
