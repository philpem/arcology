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

## rebuild-similarity

Rebuild the artefact content-set similarity cache. Recomputes size-weighted
Jaccard similarity between artefacts — and between directory-subtree
"components" (e.g. RISC OS `!Apps`) — from the current `ExtractedFile` data,
replacing the `artefact_similarity`, `artefact_components`, and
`component_similarity` tables.

Two artefacts are scored by how much of their *decoded file content* they
share, not by their container bytes, so differing compression (Spark vs ZIP) or
floppy flux timing noise does not affect the result. Run after extraction has
populated file listings, or to refresh after importing more artefacts.

```bash
flask rebuild-similarity
```

Results surface on each artefact page (the "Similar Artefacts" sidebar card and
the full `…/similar` page). Only file listings already present are used — run
`rescan-hashes` / analysis first if extracted-file hashes are missing.

## refresh-similarity

Incrementally refresh the similarity cache for artefacts whose extracted-file
set has changed since it was last computed (the `similarity_dirty` flag),
recomputing each one's similarity rows in full. Unlike `rebuild-similarity`
(a full O(n²) recompute), this only touches changed artefacts, so it is cheap
enough to run periodically — e.g. from cron, or via the task runner's
`TASKRUNNER_SIMILARITY_DELTA_INTERVAL`.

```bash
flask refresh-similarity                 # drain all stale artefacts
flask refresh-similarity --max-artefacts 500   # bound one run
```

Per-artefact recompute is exact for content changes. **Global** parameter changes
— a hash database's "exclude from similarity" flag, or `SIMILARITY_USE_IDF` —
affect every score and still require a full `rebuild-similarity`.

## backfill-tlsh

Compute artefact-level TLSH fuzzy hashes for existing artefacts (uploaded before
TLSH was added). TLSH enables byte-level *near-duplicate* detection of individual
files — "which one file changed between two otherwise-identical discs?".

```bash
flask backfill-tlsh                # fill in missing artefact tlsh digests
flask backfill-tlsh --force        # recompute even if already set
```

Flux artefact types (SCP/DFI/A2R) are skipped — their raw bytes carry timing
noise. Requires the optional `py-tlsh` library (installed in the Docker images).
`ExtractedFile` TLSH cannot be backfilled without re-extraction; it populates
going forward as new artefacts are analysed.

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

## reconcile-counts

Rebuild the denormalised partition counters (`total_files`, `unique_files`)
from the actual extracted-file rows. These are caches kept for cheap display
and the "partitions with files" filter; they are maintained incrementally and
refreshed on hash rescans, but this command rebuilds them from scratch if they
ever drift. Safe to re-run at any time.

(`HashDatabase.file_count` is a derived value with no stored column, so it
never needs reconciling.)

```bash
flask reconcile-counts                       # rebuild all partition counters
flask reconcile-counts --dry-run             # report drift without writing
flask reconcile-counts --batch-size 1000     # larger commit batches
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

## backfill-blobs

Assigns blob records to hashed artefacts that have none. After the global
blob deduplication migration (`00006a25a2c0`), artefacts whose SHA-256 was
NULL at migration time (not yet computed by CHECKSUM_COMPUTE) have no blob
record. Once the worker fills in their hashes, run this to create the
missing records without a full re-analysis. Safe to re-run — artefacts that
already have a blob are skipped.

```bash
flask backfill-blobs                  # assign missing blob records
flask backfill-blobs --dry-run        # preview without changes
flask backfill-blobs --batch-size 200 # tune commit batch size
```

Options:

| Flag | Description |
|------|-------------|
| `--batch-size N` | Number of rows to commit per batch (default: 500) |
| `--dry-run` | Show what would be updated without making changes |

## dedup-artefacts

Reports repeated artefact content and optionally prunes legacy duplicate
storage objects left over from uploads that predated the blob deduplication
migration. Artefact rows are never deleted — identical content may
intentionally exist as separate artefacts with different owners, privacy,
labels, and lineage. Only orphaned physical files (storage paths that do not
match any blob record) are removed.

```bash
flask dedup-artefacts          # report duplicates and preview removals
flask dedup-artefacts --apply  # delete non-canonical objects from storage
```

Options:

| Flag | Description |
|------|-------------|
| `--apply` | Delete non-canonical legacy objects from the storage backend |
