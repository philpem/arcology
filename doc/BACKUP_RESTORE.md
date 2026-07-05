# Backup and Restore

Arcology's state lives in two places, and **both** must be backed up together to
get a consistent restore:

1. **PostgreSQL** — the catalogue (items, artefacts, analyses, hashes, users).
2. **The `data/` volumes** — the actual bytes:
   - `data/uploads/` — original uploaded artefacts (**irreplaceable**).
   - `data/outputs/` — analysis outputs / transcodes (regenerable by
     re-analysis, but expensive).
   - `data/chunks/` — in-progress chunked uploads (transient; safe to skip).

The database references storage objects by content hash / path, so a database
dump taken at a very different time from the file backup can point at files that
don't exist yet (or vice versa). Prefer taking both close together; for a fully
consistent snapshot, stop the stack first.

## Database backup

```bash
# Logical dump (portable across PostgreSQL versions — recommended)
docker compose exec -T db pg_dump -U arcology_user -Fc arcology > arcology-$(date +%F).dump
```

`-Fc` is the custom format (compressed, restorable with `pg_restore`). Store the
dump off-host.

## File backup

```bash
tar czf arcology-data-$(date +%F).tar.gz data/uploads data/outputs
```

`data/uploads` is the only truly irreplaceable part — prioritise it.

## Restore

```bash
# 1. Restore files
tar xzf arcology-data-YYYY-MM-DD.tar.gz

# 2. Restore the database into a fresh, empty cluster
docker compose up -d db
docker compose exec -T db dropdb -U arcology_user --if-exists arcology
docker compose exec -T db createdb -U arcology_user arcology
docker compose exec -T db pg_restore -U arcology_user -d arcology --no-owner < arcology-YYYY-MM-DD.dump

# 3. Bring the rest up
docker compose up -d
```

## PostgreSQL major-version upgrades — read before pulling

The `db` service uses `pgautoupgrade`, which performs a **one-way, irreversible**
`pg_upgrade` of the on-disk cluster when it starts against data from an older
major version. `docker-compose.yml` pins the image to `18-alpine` by default
(the version `:latest` currently resolves to) rather than tracking `:latest`,
which would roll the major forward on every re-pull and silently rewrite your
data with no rollback. Override `POSTGRES_IMAGE_TAG` in your `.env` only if your
cluster runs a different version, e.g. `POSTGRES_IMAGE_TAG=17-alpine`.

Pinning constraints:

- Pin **at or above** the running version — `pgautoupgrade` only upgrades; a tag
  *below* the on-disk version fails to start trying to "downgrade".
- Keep the **same variant** (Alpine vs Debian/bookworm). `:latest` is Alpine
  (musl); switching to a glibc build changes text collation and can corrupt
  index ordering.

To upgrade PostgreSQL deliberately:

1. **Take a full logical dump first** (see above) — this is your only rollback.
2. Raise `POSTGRES_IMAGE_TAG` in `.env` (e.g. `17-alpine` → `18-alpine`).
3. `docker compose up -d db` and watch the logs for the upgrade to complete.
4. Verify the app, then keep the dump until you're confident.

> ⚠️ `docker compose down -v` **deletes the named/anonymous volumes** and can
> destroy the cluster. Use `docker compose down` (without `-v`) for routine
> stops, and always have a current dump before any volume operation.
