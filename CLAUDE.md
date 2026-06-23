# CLAUDE.md - Arcology Development Guide

## Project Overview

Arcology is a Flask-based digital artefact catalogue for retrocomputing
collections: cataloguing, uploading, and automatic analysis of disk images,
flux dumps, and archives from historical computer media.

Four components:
- **Web app** (`myapp/`) — Flask UI + REST API.
- **Analysis worker** (`worker/`) — standalone process; polls for jobs and runs
  external tools (*data plane*: needs the upload/output volumes). **No direct DB
  access** — everything goes through the REST API.
- **Task runner** (`myapp/taskrunner/`, run via `flask taskrunner`) —
  single-instance in-process loop reusing the web image. Owns the DB-only
  *control-plane* analyses and periodic maintenance, with **direct DB access, no
  HTTP**.
- **CLI** (`cli/`) — `arco` client for creating items and uploading artefacts.

Shared definitions (enums, archive formats) live in `arcology_shared/` and are
imported by all components.

## Architecture

```
CLI (arco)  --> HTTP/JSON -->  Web (Flask)  <-- HTTP/JSON -->  Worker (Python)
                                  |   ^                            |
                                  |   | in-process (no HTTP)       | Shared volumes
                       SQLAlchemy |   | SQLAlchemy                 v
                                  v   |                       uploads/ & outputs/
                               PostgreSQL <-- Task runner (flask taskrunner)
```

- Shared volumes: `data/uploads/` (originals) and `data/outputs/` (results); the
  task runner also mounts `data/chunks/` for chunked-upload GC.
- Job claiming is atomic and duplicate-safe: workers via `PUT /api/analysis/{id}`;
  the task runner via an in-process `UPDATE ... WHERE status=PENDING` rowcount check.

### Control plane vs data plane

`arcology_shared/enums.py` defines `CONTROL_PLANE_ANALYSIS_TYPES` — DB-only
analyses (`HASH_RESCAN`, `PRODUCT_RECOGNITION`, `HASHDB_LINK`, `HASHDB_DELETE`,
`HASHDB_RECOGNITION`, `SIMILARITY_REFRESH`). The **task runner** owns them
end-to-end in-process; the **worker hard-excludes** them
(`AnalysisWorker._effective_types()`). No analysis is dispatched over HTTP.

- Run-to-completion drivers live in `myapp/services/hashdb_jobs.py`
  (`run_hashdb_link_job`, `run_hashdb_delete_job`, `run_hashdb_recognition_job`,
  `run_hash_rescan_job`, `run_partition_recognition_job`) — sole owners of the
  delete state machine (`delete_one_step`) and recognition finaliser
  (`finalise_recognition_status`). Recognition runs each step under a PostgreSQL
  `statement_timeout` (`TASKRUNNER_RECOGNITION_STATEMENT_TIMEOUT`, default 300s);
  on timeout the runner skips that one product (`recognition_batch_last_id`)
  rather than failing the whole backfill. `SIMILARITY_REFRESH`'s driver is
  `run_similarity_refresh_job` in `myapp/services/similarity.py`.
- Claim eligibility (incl. the CLEANUP re-analysis barrier) and stale-reset live
  in `myapp/services/analysis_queue.py` (`pending_claimable_query()`,
  `reset_stale_analyses_core()`), shared by the worker-poll endpoint and runner.
- The task runner is **single-instance** — don't scale it. Intervals are
  `TASKRUNNER_*` config keys. To add a DB-only analysis type, add it to
  `CONTROL_PLANE_ANALYSIS_TYPES` **and** `DISPATCH` in
  `myapp/taskrunner/dispatch.py` (an import-time assert enforces the pairing).

## Repository Structure

```
arcology/
├── myapp/                      # Flask web application
│   ├── app.py                  # Application factory (create_app)
│   ├── database.py             # All SQLAlchemy models
│   ├── enums.py                # Web-specific enums (AnalysisStatus, RestrictionType, UserPermission, …)
│   ├── extensions.py           # Flask extension instances
│   ├── visibility.py           # Access-control helpers
│   ├── permissions.py          # Route decorators (@public_readable, @require_visible_item, …)
│   ├── riscos_filetypes.py     # RISC OS filetype mapping
│   ├── blueprints/             # Feature modules (auto-discovered): dashboard, items,
│   │                           #   artefacts, auth, oidc_auth, taxonomy, analysis, search, api
│   ├── cli/                    # Flask CLI commands (create-admin, taskrunner, maintenance)
│   ├── taskrunner/             # In-process control-plane loop (runner.py, dispatch.py)
│   ├── services/               # Shared service layer (artefact_types, upload_pipeline,
│   │                           #   artefact_lifecycle/storage, restrictions, downloads,
│   │                           #   hash_rescan, hashdb_jobs, analysis_queue, search_index, similarity)
│   ├── templates/              # Jinja2 templates (Bootstrap 5)
│   └── static/                 # CSS
├── arcology_shared/            # Shared: enums.py, archive_formats.py, artefact_types.py, storage.py
├── worker/                     # Analysis worker (separate container)
│   ├── worker.py               # Entry point
│   ├── Dockerfile              # Multi-stage build compiling external tools (slow)
│   └── arcworker/              # analysis.py (job handlers), api.py, config.py, compression.py, tools/
├── cli/                        # arco client (arccli/: main, client, config, formatting, commands/)
├── docker-compose.yml          # Full stack: web + worker + PostgreSQL
├── Dockerfile / Dentrypoint.sh # Web container + startup (db migrate + gunicorn)
├── requirements.txt
├── doc/  devtools/             # Docs and dev utilities
└── CONTRIBUTING.md  README.md
```

## Git Workflow and Branch Naming

**Never use tool-generated or `claude/`-prefixed branch names.** A branch name
must describe the work at a high level and state its kind.

Format: `<type>/<short-description>[-ghNNN]`

- **Lowercase, hyphen-separated, alphanumeric only** (a–z, 0–9, hyphens). No
  spaces, underscores, punctuation, double hyphens, or trailing hyphens.
- **Descriptive but concise** — reflect what the branch does.
- `ghNNN` optionally references a GitHub issue (e.g. `gh282` → issue 282).

Prefixes:

| Prefix      | Use for | Example |
|-------------|---------|---------|
| `feature/`  | New functionality | `feature/adfs-reticulate-splines` |
| `fix/`      | Bug fixes | `fix/item-deletion` |
| `hotfix/`   | Urgent production fixes | `hotfix/critical-auth-bypass` |
| `perf/`     | Performance improvements | `perf/db-query-optimise-gh282` |
| `refactor/` | Restructuring without behaviour change | `refactor/upload-pipeline` |
| `docs/`     | Documentation only | `docs/archive-extraction` |
| `release/`  | Release preparation | `release/v2.0.1` |

Commit or push only when asked. Branch off the default branch before committing
to it.

## Development Commands

### Local development (web only)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp myapp/myapp.cfg.example myapp/myapp.cfg   # SECRET_KEY auto-generates in dev
flask db upgrade                             # Apply migrations / create schema
flask create-admin                           # Create admin user
python -m myapp                              # http://localhost:5000
```

### CLI tool

```bash
pip install -e cli/        # Installs "arco"
arco configure             # Server URL + API key (interactive)
arco health                # Verify connectivity
arco items create --name "My Item"
arco upload ITEM_UUID file.scp     # or --dir ./imgs for bulk
arco download ARTEFACT_UUID
```

Config: `~/.config/arcology/config.ini`, `ARCOLOGY_URL`/`ARCOLOGY_API_KEY` env
vars, or `--server`/`--api-key` flags.

### Docker (full stack)

```bash
mkdir -p data/uploads data/outputs data/db data/chunks
docker compose up --build -d
docker compose up -d --scale worker=4    # Multiple workers
docker compose logs -f web | worker
docker compose down
```

- Web UI: http://localhost:8000. Adminer (DB browser) is opt-in via
  `docker-compose.adminer.yml` (http://localhost:8080).
- For non-interactive admin creation, set `ADMIN_USERNAME`/`ADMIN_PASSWORD` in
  `.env`; `flask create-admin` reads them automatically.

### Migrations (Flask-Migrate / Alembic)

```bash
flask db migrate -m "Description"   # Generate (review it — see Database changes)
flask db upgrade / downgrade        # Apply / undo
flask db current                    # Current revision
```

### Maintenance commands

```bash
flask rebuild-search-index          # Rebuild search index
flask rebuild-similarity            # Full similarity cache rebuild
flask refresh-similarity            # Incremental refresh
flask rescan-hashes [--artefact UUID] [--batch-size N]   # Re-link against hash DBs
flask reanalyse --all | --artefact-type SCP | --platform … | --tag … [--dry-run]
flask cancel-analysis --all [--include-running] | --artefact UUID
flask reassign-ownership --from alice --to bob [--dry-run]
flask backfill-blobs [--dry-run]    # Fill blob records for late-hashed artefacts
flask dedup-artefacts [--apply]     # Remove orphaned non-canonical storage objects
```

See `doc/ADMIN_COMMANDS.md` for the full flag reference.

### Debug tools

- `devtools/run_debug.py` — Flask debug mode with auto-reload.
- `devtools/db_branch_switch.py` — downgrade DB before switching branches (see
  `doc/BRANCH_DB_SWITCHING.md`).
- `DEBUG_DB_LOG` / `DEBUG_DB_PROFILING` in `myapp.cfg` — SQL logging / profiling.

## Code Style and Conventions

- **Indentation**: 4 spaces (PEP 8). Files end with `# vim: ts=4 sw=4 et`.
- **Python 3.10+** (PEP 585 type hints in newer code).
- **Linting**: CI runs `ruff check .` over the **entire repo**. Run it (and
  `ruff check --fix .`) before committing; the pre-push hook enforces it. Most
  common issues: unsorted imports (I001), undefined names (F821).
- **Avoid legacy SQLAlchemy APIs.** The app-tests runner escalates
  `DeprecationWarning` (incl. SQLAlchemy `LegacyAPIWarning`) to a hard error, so
  legacy calls that only warn locally **fail in CI**. Use `db.session.get(Model, pk)`
  not `Model.query.get(pk)`; prefer 2.0-style `db.session.scalars(db.select(...))`.
  Reproduce CI with `python ci/run_app_tests.py` (plain `unittest` won't escalate).
- **UUIDs for public identifiers** — URLs and API responses use UUID hex, never
  sequential integer IDs (no IDOR).
- **Application factory** — `create_app()` in `app.py`; extensions bound in the
  factory, not at import time.
- **Blueprint auto-discovery** — any module in `myapp/blueprints/` with a
  `blueprint` variable is auto-registered (optional `init_app(app)`).
- **Single model file** — all models in `myapp/database.py`. Add web enums in
  `myapp/enums.py` (re-exported from `database.py`).
- **Shared enums/formats** — edit `arcology_shared/` only (see below).
- **CSRF** — global via Flask-WTF; the API blueprint exempts itself in `init_app()`.
- Passwords are bcrypt-hashed.

## Public Mode and Access Tiers

Config keys (set in `myapp.cfg` as Python booleans or as env vars):

| Key | Default | Description |
|-----|---------|-------------|
| `PUBLIC_MODE` | `False` | Anonymous read-only browsing of non-private content. |
| `PUBLIC_DOWNLOADS` | `True` | Anonymous downloads (only when PUBLIC_MODE on). |

Tier hierarchy:
`anonymous (PUBLIC_MODE) < READ_ONLY < READ_WRITE < STAFF < admin (is_admin=True)`.
Browse: all tiers. Upload/edit + taxonomy: READ_WRITE and up. User management:
admin only. (`OIDC_ROLE_STAFF`, default `arcology-staff`, maps SSO users to STAFF.)

### Decorators (`myapp/permissions.py`)

- `@public_readable` — like `@login_required` when PUBLIC_MODE is off; lets
  anonymous through when on. **Read-only GET routes only**; write routes keep
  `@login_required`.
- `@public_downloadable` — same, additionally checking `PUBLIC_DOWNLOADS`.
- `@require_visible_item` — resolves the route slug/UUID, 404s if the user can't
  view the item (private items must be indistinguishable from nonexistent),
  optionally 403s non-contributors (`contribute=True`), and passes the resolved
  `item` kwarg. Apply **below** the auth decorator:

  ```python
  @blueprint.route('/<string:uuid>/edit', methods=['GET', 'POST'])
  @login_required
  @require_permission('read_write')
  @require_visible_item(contribute=True)
  def edit(uuid, item): ...
  ```

  For secondary lookups in a view body, use `ensure_visible_item(item)` /
  `ensure_item_contribution(item)`. List/aggregate queries must filter with
  `item_visibility_clause(current_user)` / `artefact_visibility_clause(...)` from
  `myapp/visibility.py` (see `_visible_analyses_query()` in
  `myapp/blueprints/analysis.py`). All decorators respect `LOGIN_DISABLED`.

> **Recurring security bug class — visibility-filter omission on aggregate /
> cross-cutting queries.** Per-object guards are reliable; bugs cluster where
> code queries *across all data* and forgets the filter the per-object path has.
> Check that:
> - every `func.count(...)` / `.count()` / aggregate joins `Item` and applies a
>   `*_visibility_clause`;
> - `get_all_derived_artefact_ids(...)` results are re-filtered by visibility (a
>   *derived* artefact can be `is_private` even when its root is public);
> - worker-poll / operational endpoints returning storage paths or system-wide
>   rows are gated by `_is_worker_request()` (not just `read_only`);
> - outputs/renderings of restricted artefacts use `output_blocked_for` /
>   `can_download_despite_restrictions`, not visibility alone;
> - CSV exports of user text neutralise spreadsheet formula injection.

### Download restrictions and explicit (NSFW) content

Restrictions are an access gate **distinct from visibility**. Visibility controls
who can *see an artefact exists*; restrictions control who can *download the
bytes* — including any rendering of those bytes.

`RestrictionType` (`myapp/enums.py`): `MALWARE`, `PII`, `COPYRIGHT`,
`LEGAL_HOLD`, `EXPLICIT`, `CORRUPTED`. They attach to an artefact
(`ArtefactRestriction`) or extracted file (`ExtractedFileRestriction`).

**Bypass model** — a user passes a restriction via a global per-type grant
(`User.can_bypass_restriction(type)`) or a per-artefact grant
(`UserArtefactBypass`) that **cascades down the derived-from chain**. Admins
bypass everything; anonymous and the worker key never bypass.

**Single source of truth — use these, don't hand-roll** (`myapp/visibility.py`):
- `can_download_despite_restrictions(user, restrictions, artefact)` — gates raw
  download routes and the REST API equivalents.
- `output_blocked_for(user, artefact)` — its inversion over
  `artefact.effective_restrictions`. **Analysis outputs are renderings of
  restricted bytes**, so every route serving/linking an output applies it
  (`get_output_file` web + API both `abort(403)`), and the viewer must not emit
  media/poster URLs for a blocked artefact — show a locked placeholder instead.
- `content_gate_flags(user, artefact) -> (restricted, explicit)` — the display
  flags. `restricted` = hard lock (locked placeholder/notice); `explicit` =
  `(not restricted) and can_reveal_explicit(user) and art is effectively
  EXPLICIT` (soft blur). Mutually exclusive. **Gate per *owning* artefact, not
  just the viewed root.**

**`EXPLICIT` is a *soft* gate** layered on the hard one: users who can't bypass
get a hard block; users who can see the content **blurred behind a "click to
reveal" consent overlay** (`revealExplicit()` in viewer.html).

**Render gates from one place** — the macros in
`myapp/templates/artefacts/_content_gates.html` are the single source of gate
markup; don't hand-write overlay/blur/placeholder HTML:
- `{% call explicit_gate(stable_id, compact=…) %}…media…{% endcall %}`
- `locked_thumb(title=…)` — hard-locked thumbnail placeholder.
- `restricted_notice(subject, what, level=…)` — inline "withheld" alert.

The Acorn Replay viewer is the worked example (`content_gate_flags()` in
`_viewer_replay_*`, the `replay_*` macros; covered by
`ci/test_output_restrictions.py`).

## Key Patterns

### Adding a blueprint
Create `myapp/blueprints/yourfeature.py` with a `blueprint` variable — it's
auto-registered by `_register_blueprints()` in `app.py`.

### Adding an analysis type
1. Add to `AnalysisType` in `arcology_shared/enums.py`.
2. Add to `ANALYSIS_MAP` in `myapp/services/artefact_types.py`.
3. Implement the handler in `worker/arcworker/analysis.py`.
4. Migration: `ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'MY_NEW_TYPE'`
   (see Database changes — uppercase NAME, autocommit block, cleanup downgrade).

### Adding an artefact type
1. Add to `ArtefactType` in `arcology_shared/enums.py`.
2. Add to `EXTENSION_MAP` in `arcology_shared/artefact_types.py`.
3. Add to `ANALYSIS_MAP` in `myapp/services/artefact_types.py`.
4. Migration: `ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'NEWTYPE'`.

### Adding a flux format that converts to SCP
Some flux formats (DFI, A2R, …) must first convert to SCP, after which the SCP
pipeline runs unchanged. The commit *"Add A2R flux image support via SCP
conversion path"* is a minimal worked example. Checklist (use DFI/A2R as
reference):
1. `arcology_shared/enums.py` — add the `ArtefactType` member.
2. `myapp/services/artefact_types.py` — `EXTENSION_MAP` + `ANALYSIS_MAP`
   (`FLUX_VISUALISATION`, `FLUX_DECODE`, `METADATA_EXTRACT`). If a user hint is
   needed, add fields to `ArtefactUploadForm`/`AnalyseForm` in
   `myapp/blueprints/artefacts.py` and the templates.
3. `worker/arcworker/tools/flux.py` — `newtype_to_scp_<tool>()` returning the
   standard result dict (model on `dfi_to_scp_hxcfe()`; A2R: `gw convert in.a2r out.scp`).
4. `worker/arcworker/tools/__init__.py` — export it.
5. `worker/arcworker/analysis.py` — import it; add the type to
   `_SCP_VIA_CONVERSION_TYPES`; add `elif` branches in
   `process_flux_visualisation()` and `process_flux_decode()` (register the SCP
   sibling with **no** `skip_analyses`); add to `_PROMOTABLE_EXTENSIONS`.
6. Hand-crafted migration adding the enum value.
7. `ci/test_flux_decode.py` — add a `TestNEWTYPESource` mirroring `TestDFISource`.

### Adding an archive format
1. Add to `ArchiveType` in `arcology_shared/archive_formats.py`.
2. Add to `ARCHIVE_FORMATS` (same file).
3. Add an extraction branch in `process_archive_extract` (`worker/arcworker/analysis.py`).
4. Update `doc/ARCHIVE_EXTRACTION.md`.

### Analysis pipeline flow
Upload → auto-analysis (`ANALYSIS_MAP`) → worker claims atomically → runs tools
→ reports via API → derived artefacts trigger follow-on analyses (flux → decode
→ file listing).

## Database Changes

1. Edit models in `myapp/database.py`.
2. `flask db migrate -m "Description"`.
3. **Review the migration** — Alembic can't detect renames (shows drop+create),
   enum changes, or some constraint changes.
4. `flask db upgrade`.

### Adding values to a PostgreSQL enum

Two non-negotiable rules (CI enforces both via `ci/check_migration_sanity.py`):

**1. `ALTER TYPE ... ADD VALUE` can't run in a transaction** — wrap it in
`autocommit_block()`. **Use the UPPERCASE enum NAME**, not the lowercase
`.value` — SQLAlchemy stores members by `.name`, so the DB holds e.g.
`'FILE_EXTRACTION'`, and the wrong case fails at runtime with `invalid input
value for enum`.

```python
def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'NEW_TYPE'"))
    # Any remaining transactional DDL goes here
```

**2. `downgrade()` MUST clean up rows.** PostgreSQL can't drop an enum value, and
the ORM crashes with `LookupError` reading a row whose enum holds a value absent
from the Python enum. So delete/remap rows using the new value:

```python
def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # AnalysisType: null FK refs first, then delete
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'MY_NEW_TYPE')"""))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'MY_NEW_TYPE'"))
```

For `ArtefactType` use the `_cascade_sql()` helper (see `doc/MIGRATION_PATTERNS.md`);
for `FilesystemType` remap to `'UNKNOWN'`. A `_TolerantEnum` TypeDecorator
crash-shields orphan rows (returns `None`), but the downgrade cleanup is the
proper fix. The same applies to any `enum.Enum`-backed column.

### Revision IDs and filenames

- **Never use placeholder/patterned IDs** (`a1b2c3d4e5f6`). Generate from time:
  `python3 -c "import time; print(hex(int(time.time()))[2:].zfill(12))"`. Keep
  Alembic-generated `revision` values unchanged.
- **Filename**: `YYYYMMDD_HHMMSS_description.py` (UTC). The timestamp is an
  **ordering key** — lexicographic filename order must match the `down_revision`
  chain (CI checks this). Don't use mtime, `Create Date`, or the raw hex revision
  as the prefix.
- **Collapsing before merge**: when a branch accumulates dev-artefact migrations,
  collapse to one — keep the head `revision` ID, set `down_revision` to the
  pre-branch head, delete intermediates, align the filename timestamp.
- **Branch conflicts**: two PRs extending the same head create "Multiple head
  revisions." CI detects this (`ci/check_migration_conflict.py`); a pre-push hook
  is in `hooks/`. See `doc/MIGRATION_CONFLICTS.md`.

## RISC OS INF Sidecar Processing

Some BBC Micro / RISC OS tools (currently Disc Image Manager) emit `.inf`
sidecars carrying metadata Unix filesystems can't hold: load/exec address, RISC
OS filetype, attributes, original BBC filename.

`process_inf_sidecars(output_dir)` in `worker/arcworker/tools/extraction.py` is
the reusable step. Call it **before** `enumerate_extracted_files()` so files are
renamed and metadata collected before hashing; it returns a `dict[str, dict]`
passed as `enumerate_extracted_files(inf_metadata=…)`. Each tool that emits INF
files calls it and returns the result as `'inf_metadata'`. It:
1. walks for `.inf` files; 2. validates a matching data file; 3. parses
   `<filename> <load_hex> <exec_hex> [<length_hex>] [<access>]`; 4. derives the
   filetype from the load address when date-stamped (top 12 bits = `0xFFF`);
   5. renames DOS-encoded → BBC name; 6. deletes the INF; 7. returns metadata.

BBC↔DOS character translation (`_BBC_TO_DOS`/`_DOS_TO_BBC`, inverses — edit both):
`# ↔ ?`, `. ↔ /`, `$ ↔ <`, `^ ↔ >`, `& ↔ +`, `@ ↔ =`, `% ↔ ;`.

DB fields populated: `load_address`/`exec_address` (String(8), zero-padded hex),
`risc_os_filetype` (String(3), from load bits 19:8), `attributes` (String(50),
as-is). To extend to a new tool: emit standard `.inf` files, call
`process_inf_sidecars()`, pass `inf_metadata` through; for a different
translation table pass `translation_table=` or normalise first; for a different
INF format extend `_parse_inf_line()`. Covered by `ci/test_inf_processing.py`.

## Important Files

| File | Role |
|------|------|
| `arcology_shared/enums.py` | `ArtefactType`, `AnalysisType` — single source of truth |
| `arcology_shared/archive_formats.py` | `ArchiveType`, `ARCHIVE_FORMATS` |
| `arcology_shared/artefact_types.py` | `EXTENSION_MAP` (type detection) |
| `arcology_shared/storage.py` | Storage backends (`LocalStorage`/`S3Storage`, via `STORAGE_BACKEND`) |
| `myapp/database.py` | All SQLAlchemy models |
| `myapp/enums.py` | Web-specific enums |
| `myapp/services/artefact_types.py` | `ANALYSIS_MAP` and analysis scheduling |
| `myapp/visibility.py` | Access-control helpers (`*_visibility_clause`, restriction gates) |
| `myapp/permissions.py` | Route decorators |
| `myapp/blueprints/search.py` | Global search (`parse_query()`, `_run_search()`) |
| `myapp/blueprints/api.py` | REST API for workers and CLI |
| `myapp/riscos_filetypes.py` | RISC OS filetype mapping (`lookup_filetype_hex()`) |
| `worker/arcworker/analysis.py` | Worker job handlers |
| `worker/arcworker/tools/extraction.py` | Extraction, INF sidecars, BBC↔DOS translation |
| `cli/arccli/main.py`, `client.py` | CLI entry point + HTTP client |
| `myapp/app.py` | App factory, error handlers, blueprint registration |

## Testing

Tests live in `ci/` and run in the `app-tests` job (SQLite in-memory):

| Test | Covers |
|------|--------|
| `test_app_smoke.py` | App start, `/api/health`, API key auth |
| `test_search.py` | `parse_query()`, `lookup_filetype_hex()`, `_run_search()`, HTTP smoke |
| `test_artefact_map.py` | `EXTENSION_MAP`/`ANALYSIS_MAP` consistency |
| `test_archive_formats.py` | Archive format completeness |
| `test_slug.py`, `test_url_identifiers.py` | Slugs / URL-safe identifiers |
| `test_idempotency.py` | Pipeline idempotency (no duplicate rows) |
| `test_checksum_compute.py` | Hash computation |
| `test_fk_violations.py` | FK cascade deletes, M2M cleanup, nullable FK edges |
| `test_inf_processing.py` | INF parsing, BBC↔DOS translation, `process_inf_sidecars()` |
| `test_chunked_upload.py`, `test_chunked_finalize.py`, `test_cli_chunked.py` | Chunked upload (sync + async + CLI) |
| `test_worker_io.py` | Bounded-memory access (`SectorReader`, `read_file_capped`, sparse-image regression) |
| `test_similarity.py` | Content-set similarity, visibility filtering |
| `test_taskrunner.py` | Control-plane classification, atomic claim, CLEANUP barrier, drivers |
| `test_output_restrictions.py` | Output/restriction gates (incl. Replay) |

Run locally:

```bash
SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
    python -m unittest discover -s ci -p "test_*.py" -v
```

Also verify manually when relevant: web CRUD/upload/search, API endpoints,
analysis pipeline, and migrations (both directions).

## Dependencies

Python (`requirements.txt`): Flask, SQLAlchemy, Flask-SQLAlchemy, Flask-Migrate,
Flask-Login, Flask-WTF, bootstrap-flask, bcrypt, python-dotenv, requests,
psycopg2-binary, watchdog.

Worker external tools (compiled in the worker Dockerfile): Fluxfox (Rust), HxCFE
(C), Greaseweazle (Python), DiscImageManager (Pascal), 7z, fcfs2raw,
scotch `replay-transcode` + ffmpeg (Acorn Replay → MP4; see
`doc/REPLAY_TRANSCODE.md`).

## Common Gotchas

- **Switching branches with migrations**: the DB schema is independent of git.
  Run `python devtools/db_branch_switch.py [target-branch]` to downgrade first
  (see `doc/BRANCH_DB_SWITCHING.md`).
- **Migration squash watermark**: the 47 pre-squash migrations were consolidated
  into `20260604_223020_initial_migration.py` (revision `00006a21fc7c`). An
  instance stamped at an older individual revision can't `flask db upgrade` with
  current code — bring it to `00006a21fc7c` first using the original code.
- **Config**: `myapp.cfg` is optional; env vars take precedence.
  `SQLALCHEMY_DATABASE_URI`, `SECRET_KEY`, `WORKER_API_KEY` are read from the
  env if not in the cfg. `SECRET_KEY` auto-generates (with a warning) if missing
  or too short — set it explicitly for persistent sessions.
- The Docker entrypoint runs `flask db upgrade` + `flask create-admin` on every
  start (both idempotent). `create-admin` reads `ADMIN_USERNAME`/`ADMIN_PASSWORD`
  non-interactively; passwords must be ≥12 chars.
- Running the worker outside Docker: run from the repo root (`python
  worker/worker.py`) so `arcology_shared/` is importable.
- Upload limit is 4GB (`MAX_CONTENT_LENGTH`).
- **Never load a whole artefact into RAM unless you know it fits.** Artefacts are
  routinely multi-GB; `read_bytes()` / `f.read()` / `bytearray(...)` over an
  unbounded file is an OOM/DoS (a 6 GB ADFS disc OOM-killed a worker). In worker
  code:
  - **Random access into a large image**: `open_sector_reader(path)` →
    `SectorReader` (`worker/arcworker/tools/base.py`) — `len()`, indexing, slicing
    via bounded seek/read; **slice before `struct.unpack_from`** (no buffer
    protocol). `fs_riscos_armlock.py` is the worked example.
  - **A known bounded region** (header, sector, sprite): seek and read exactly
    that many bytes after a sanity cap.
  - **An inherently small whole file**: `read_file_capped(path)` — raises
    `FileTooLargeError` (an `OSError`) above `MAX_INMEM_BYTES` (256 MiB).
  - **In-place edits of a large image**: `shutil.copy()` then patch sectors with
    seek/read/write — never read-modify-write in memory.
  - Covered by `ci/test_worker_io.py`.
- **Stale jobs**: `STALE_JOB_TIMEOUT_SECONDS` (default 3600) is the max silence
  before a `RUNNING` job is reset to `PENDING`. Staleness is heartbeat-based
  (`COALESCE(progress_updated_at, started_at) < now − timeout`). The worker's
  cancel-monitor heartbeats every `CANCEL_CHECK_INTERVAL` (30s) and handlers bump
  `progress_updated_at` via `self.progress`, so set the timeout to "max
  acceptable silence", comfortably above `CANCEL_CHECK_INTERVAL`. The monitor
  heartbeat caps at `HEARTBEAT_MAX_SECONDS` (default 6h) so a wedged handler
  eventually becomes eligible. Stale jobs are re-queued at startup and every
  `STALE_RESET_INTERVAL` (300s). Reset is a full re-run (safe — the pipeline is
  idempotent).
- **S3 Content-Type must be set at upload, not inferred at read.** S3 backends
  don't auto-detect MIME from the key, so an object uploaded without `ContentType`
  downloads instead of rendering inline. `S3Storage.put()`/`put_tree()` already
  call `mimetypes.guess_type()`; `presigned_url()` sets `ResponseContentType`. For
  a new output format: ensure the filename has the right extension and
  `mimetypes` maps it (call `mimetypes.add_type(...)` in
  `arcology_shared/storage.py` for unusual types), and pass `mimetype=` to
  `send_file()` in both `get_output_file()` implementations.
- **Do NOT use Python's `zipfile` on RISC OS ZIPs.** They contain Acorn extra
  fields (ID `0x4341`) that `zipfile` rejects with `BadZipFile`. Parse the
  central directory with `struct` or shell out to `unzip` (see `_is_riscos_zip()` /
  `extract_zip_riscos()`).
- **Bootstrap 5 collapse + `stopPropagation` is unreliable** (document-level
  delegation). Remove `data-bs-toggle`/`data-bs-target`, add a row `click`
  listener that bails on `event.target.closest('.actions-class')`, and call
  `bootstrap.Collapse.getOrCreateInstance(el, {toggle:false}).toggle()`. See
  `myapp/templates/hashdb/view.html`.
- **Jinja2 `{% block %}` inside `{% if %}` is unreliable** (blocks resolve at
  parse time, `if` at render time). Put the `if` *inside* the block:
  ```jinja
  {% block scripts %}{{ super() }}{% if condition %}…{% endif %}{% endblock %}
  ```
