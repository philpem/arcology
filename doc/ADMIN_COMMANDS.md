# Admin Commands

Flask CLI commands for managing the Arcology instance. In Docker, prefix
each command with `docker compose exec web`.

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

## rescan-hashes

Re-link extracted files to active hash databases without re-analysing.
Useful after importing new hash databases.

```bash
flask rescan-hashes                          # all artefacts
flask rescan-hashes --artefact <UUID>        # single artefact
flask rescan-hashes --batch-size 1000        # larger batches
```

## reanalyse

Reset and re-queue analysis for artefacts. Clears all previous analysis
results, derived artefacts, and output files, then queues fresh analyses
based on each artefact's type.

At least one filter or `--all` is required.

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
| `--all` | Select every artefact in the database |
| `--item UUID` | Restrict to artefacts belonging to a single item |
| `--tag NAME` | Restrict to items with this tag |
| `--platform NAME` | Restrict to items on this platform |
| `--category NAME` | Restrict to items in this category |
| `--artefact-type TYPE` | Restrict to this artefact type (e.g. `SCP`, `HFE`, `IMG`) |
| `--dry-run` | Show what would be requeued without making changes |
| `--batch-size N` | Commit every N artefacts (default: 50) |
