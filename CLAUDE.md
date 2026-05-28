# CLAUDE.md - Arcology Development Guide

## Project Overview

Arcology is a digital artefact catalogue for retrocomputing collections, built on Flask. It enables cataloguing, uploading, and automatic analysis of digital artifacts like disk images, flux dumps, and archives from historical computer media.

The system has three main components connected via a REST API:
- **Web application** (`myapp/`) - Flask app serving the UI and REST API
- **Analysis worker** (`worker/`) - Standalone Python process that polls for analysis jobs and runs external tools
- **CLI tool** (`cli/`) - `arco` command-line client for creating items and uploading artefacts from a client PC

Shared type definitions (enums, archive formats) live in `shared/` and are imported by all three components.

## Architecture

```
CLI (arco)  --> HTTP/JSON -->  Web (Flask)  <-- HTTP/JSON -->  Worker (Python)
                                    |                              |
                                    | SQLAlchemy                   | Shared volumes
                                    v                              v
                               PostgreSQL                    uploads/ & outputs/
```

- The worker has **no direct database access** - all communication goes through the REST API
- The CLI tool communicates with the web app via the same REST API (authenticated with API keys)
- Shared filesystem volumes: `data/uploads/` (originals) and `data/outputs/` (analysis results)
- Workers claim jobs atomically via `PUT /api/analysis/{id}` to prevent duplicate processing

## Repository Structure

```
arcology/
├── myapp/                      # Flask web application
│   ├── app.py                  # Application factory (create_app)
│   ├── database.py             # All SQLAlchemy models and web-specific enums
│   ├── extensions.py           # Flask extension instances (db, migrate, login_manager, bootstrap, csrf)
│   ├── __main__.py             # Dev server entry point (python -m myapp)
│   ├── myapp.cfg.example       # Config template
│   ├── riscos_filetypes.py     # RISC OS filetype mapping
│   ├── blueprints/             # Feature modules (auto-discovered)
│   │   ├── dashboard.py        # Homepage with collection stats
│   │   ├── items.py            # Item CRUD (search, filter, pagination)
│   │   ├── artefacts.py        # File upload, type detection, ANALYSIS_MAP
│   │   ├── taxonomy.py         # Platforms, categories, tags, external systems
│   │   ├── analysis.py         # Analysis queue UI
│   │   ├── search.py           # Global cross-item search (prefix query syntax)
│   │   └── api.py              # REST API for workers and external tools
│   ├── cli/                    # Flask CLI commands (create-admin, rebuild-search-index, rescan-hashes, reanalyse)
│   ├── utils/                  # Utility modules
│   ├── templates/              # Jinja2 templates (Bootstrap 5)
│   └── static/                 # CSS
├── shared/                     # Shared definitions (web app, worker, and CLI)
│   ├── enums.py                # ArtefactType and AnalysisType (single source of truth)
│   └── archive_formats.py      # Archive format definitions
├── worker/                     # Analysis worker (separate container)
│   ├── worker.py               # Entry point
│   ├── Dockerfile              # Multi-stage build compiling HxCFE, Fluxfox, etc.
│   └── arcworker/              # Worker package
│       ├── analysis.py         # AnalysisWorker class and job handlers
│       ├── api.py              # HTTP client for web API
│       ├── config.py           # Environment-based config
│       ├── compression.py      # Decompression utilities
│       └── tools/              # Wrappers for external analysis tools
├── cli/                        # Command-line client (arco)
│   ├── pyproject.toml          # arcology-cli package, "arco" entry point
│   └── arccli/                 # CLI package
│       ├── main.py             # Argument parsing and command dispatch
│       ├── client.py           # ArcologyClient HTTP class
│       ├── config.py           # Configuration loading (~/.config/arcology/)
│       ├── formatting.py       # Output formatting (tables, JSON)
│       └── commands/           # Command implementations
├── docker-compose.yml          # Full stack: web + worker + PostgreSQL
├── docker-compose.adminer.yml  # Optional Adminer database browser (separate file)
├── Dockerfile                  # Web container (Python 3 Alpine + Gunicorn)
├── Dentrypoint.sh              # Web container startup (db migrate + gunicorn)
├── .env.example                # Environment variable template
├── .flaskenv                   # Flask environment defaults
├── requirements.txt            # Python dependencies
├── doc/                        # Additional documentation
├── devtools/                   # Development utilities
├── CONTRIBUTING.md             # Architecture guide and contribution workflow
└── README.md                   # Quick start and feature overview
```

## Development Commands

### Local development (web only, no worker)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp myapp/myapp.cfg.example myapp/myapp.cfg
# Edit myapp.cfg as needed (SECRET_KEY auto-generates in dev mode)
flask db upgrade                   # Apply committed migrations to create schema
flask create-admin                 # Prompts for admin username and password
python -m myapp                    # Runs on http://localhost:5000
```

### CLI tool (client PC)

```bash
pip install -e cli/                # Installs "arco" command

arco configure                     # Interactive setup (server URL + API key)
arco health                        # Verify connectivity
arco items list                    # List items
arco items create --name "My Item" # Create item
arco upload ITEM_UUID file.scp     # Upload artefact
arco upload ITEM_UUID --dir ./imgs # Bulk upload directory
arco download ARTEFACT_UUID        # Download artefact
arco platforms                     # List platforms
```

Configuration: `~/.config/arcology/config.ini`, `ARCOLOGY_URL`/`ARCOLOGY_API_KEY` env vars, or `--server`/`--api-key` flags.

### Docker (full stack)

```bash
mkdir -p data/uploads data/outputs data/db
docker compose up --build -d       # Build and start
docker compose up -d --scale worker=4  # Multiple workers
docker compose logs -f web         # Web logs
docker compose logs -f worker      # Worker logs
docker compose down                # Stop

# Maintenance
docker compose exec web flask rebuild-search-index  # Rebuild search index
docker compose exec web flask rescan-hashes         # Re-link files against hash DBs
```

- Web UI: http://localhost:8000
- Adminer (DB browser): not started by default; use `docker compose -f docker-compose.yml -f docker-compose.adminer.yml up -d` to enable on http://localhost:8080

For non-interactive admin creation (Docker / CI), set `ADMIN_USERNAME` and
`ADMIN_PASSWORD` in your `.env` file before first start. The `flask create-admin`
command reads these automatically. If no users exist after startup, run:
`docker compose exec web flask create-admin`

### Database migrations (Flask-Migrate / Alembic)

```bash
flask db migrate -m "Description of change"  # Generate migration
flask db upgrade                              # Apply migrations
flask db downgrade                            # Undo last migration
flask db current                              # Check current revision
flask db stamp head                           # Mark as up-to-date without running
```

### Maintenance commands

```bash
# Rebuild search index from completed analysis results
# (run after applying search-index migrations, or to fix inconsistencies)
flask rebuild-search-index

# Re-link extracted files against hash databases
# (run after importing a new hash database without re-running analysis)
flask rescan-hashes                     # all artefacts
flask rescan-hashes --artefact <UUID>   # single artefact
flask rescan-hashes --batch-size 1000   # tune commit batch size (default 500)

# Bulk re-queue analysis for artefacts (clears previous results)
flask reanalyse --all                   # every artefact
flask reanalyse --artefact-type SCP     # filter by type
flask reanalyse --platform "BBC Micro"  # filter by platform
flask reanalyse --tag needs-review      # filter by tag
flask reanalyse --analysis <UUID>       # retry a single analysis
flask reanalyse --all --dry-run         # preview without changes

# Cancel pending analyses without resetting artefact data
flask cancel-analysis --all             # every pending analysis
flask cancel-analysis --artefact <UUID> # all pending on one artefact
flask cancel-analysis --all --include-running  # also cancel running
```

See `doc/ADMIN_COMMANDS.md` for the full reference including all flags.

### Debug tools

- `devtools/run_debug.py` - Flask debug mode with auto-reload
- `devtools/db_branch_switch.py` - Downgrade DB to match a target branch before switching (see `doc/BRANCH_DB_SWITCHING.md`)
- `DEBUG_DB_LOG = True` in `myapp.cfg` - Log all SQL queries
- `DEBUG_DB_PROFILING = True` in `myapp.cfg` - SQL profiling reports (requires `sqltap`)

## Code Style and Conventions

- **Indentation**: 4 spaces per level (PEP 8 standard). Files end with `# vim: ts=4 sw=4 et`.
- **Python version**: 3.10+ (uses PEP 585 type hints in newer code)
- **Linting**: CI enforces [Ruff](https://docs.astral.sh/ruff/) for style and import order. Run `ruff check <files>` before committing; `ruff check --fix <files>` applies safe auto-fixes. The two most common issues are unsorted imports (I001) and undefined names from missing imports (F821).
- **UUIDs for public identifiers**: URLs and API responses use UUID hex strings, not sequential integer IDs
- **Application factory pattern**: `create_app()` in `app.py`; extensions bound in factory, not at import time
- **Blueprint auto-discovery**: Any module in `myapp/blueprints/` with a `blueprint` variable is auto-registered. Optional `init_app(app)` for additional setup.
- **Single database model file**: All SQLAlchemy models and web-specific enums live in `myapp/database.py`
- **Shared enums**: `ArtefactType` and `AnalysisType` are defined in `shared/enums.py` and imported by both `myapp/database.py` and the worker — edit only `shared/enums.py` when adding new types
- **Shared archive formats**: `ArchiveType` and `ARCHIVE_FORMATS` are defined in `shared/archive_formats.py` and imported by the worker — edit only `shared/archive_formats.py`
- **CSRF**: Enabled globally via Flask-WTF. The API blueprint exempts itself in `init_app()`.
- **Security**: bcrypt password hashing, CSRF protection, UUID-based URLs (no IDOR)

## Key Patterns

### Adding a new blueprint

1. Create `myapp/blueprints/yourfeature.py`
2. Define a `blueprint` variable (Flask Blueprint instance)
3. It will be auto-discovered and registered by `_register_blueprints()` in `app.py`

### Adding a new analysis type

1. Add to `AnalysisType` enum in `shared/enums.py`
2. Add to `ANALYSIS_MAP` in `myapp/blueprints/artefacts.py`
3. Implement handler in `worker/arcworker/analysis.py`
4. Write a migration to add the value to the PostgreSQL `analysistype` enum — **use the enum NAME (uppercase), not the value** (see "Adding values to a PostgreSQL enum" below)

> **Enum case pitfall (has caught us multiple times):** SQLAlchemy stores
> `AnalysisType` members using their `.name` — e.g. `FILE_EXTRACTION`,
> `PRODUCT_RECOGNITION` — not their `.value` (`file_extraction`,
> `product_recognition`). The PostgreSQL enum type therefore contains
> uppercase strings. Always write migrations as:
> `ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'MY_NEW_TYPE'`
> (uppercase). Using the lowercase `.value` will break at runtime with
> `invalid input value for enum analysistype: "MY_NEW_TYPE"`.
>
> **Rollback convention — downgrade() MUST clean up rows:** PostgreSQL cannot
> remove an enum value once added, but the ORM crashes with `LookupError` when
> it reads a row whose enum column holds a value absent from the Python enum.
> Every migration that adds an enum value via `ALTER TYPE ... ADD VALUE` must
> therefore have a `downgrade()` that deletes (or remaps) rows using that value.
>
> CI enforces this: `ci/check_migration_sanity.py` treats an empty `downgrade()`
> on an ADD VALUE migration as a hard error.
>
> **For `AnalysisType`** — delete the analysis rows and null out any
> `derived_from_analysis_id` references first (the FK may not have
> `ON DELETE SET NULL` at this point in the downgrade chain):
> ```python
> def downgrade():
>     bind = op.get_bind()
>     if bind.dialect.name != 'postgresql':
>         return
>     op.execute(sa.text("""
>         UPDATE artefacts SET derived_from_analysis_id = NULL
>         WHERE derived_from_analysis_id IN (
>             SELECT id FROM analyses WHERE analysis_type = 'MY_NEW_TYPE'
>         )
>     """))
>     op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'MY_NEW_TYPE'"))
> ```
>
> **For `ArtefactType`** — use the cascade helper block from any existing
> artefact-type migration (e.g. `20260324_201523_add_arc_artefact_type.py`).
> Copy the `_CASCADE_SQL` constant and call it with the type name(s):
> ```python
> op.execute(sa.text(_CASCADE_SQL).bindparams(types=['MY_NEW_TYPE']))
> ```
>
> **For `FilesystemType`** — remap to `UNKNOWN` rather than deleting:
> ```python
> op.execute(sa.text(
>     "UPDATE partitions SET filesystem = 'UNKNOWN' WHERE filesystem = 'MY_NEW_TYPE'"
> ))
> ```
>
> A `_TolerantEnum` TypeDecorator on `artefact_type` and `analysis_type` acts as
> a crash-shield for any orphan row that slips through (returns `None` instead of
> raising `LookupError`), but the proper fix is always the downgrade cleanup.

The same principle applies to any other SQLAlchemy `Enum` column backed by a
Python `enum.Enum` class in this project.

### Adding a new artefact type

1. Add to `ArtefactType` enum in `shared/enums.py`
2. Add extension mapping in `EXTENSION_MAP` in `myapp/blueprints/artefacts.py`
3. Add entries to `ANALYSIS_MAP` for auto-queued analyses

### Adding a new flux format that converts to SCP

Some flux formats (DFI, A2R, …) cannot be decoded directly by fluxfox or
greaseweazle — they must first be converted to SCP, after which the existing
SCP pipeline (HFE + IMD + RAW_SECTOR) runs unchanged on the SCP sibling.

**Worked example**: the commit titled *"Add A2R flux image support via SCP conversion
path"* (branch `claude/add-a2r-scp-conversion-MmKBo`) adds A2R support and is a
minimal, self-contained example of every step below.

**Checklist** (use DFI or A2R as the reference implementation):

1. **`shared/enums.py`** — add `NEWTYPE = "newtype"` to `ArtefactType` in the
   "Flux-level floppy images" group.

2. **`myapp/blueprints/artefacts.py`**
   - `EXTENSION_MAP`: `'.newtype': ArtefactType.NEWTYPE`
   - `ANALYSIS_MAP`: `ArtefactType.NEWTYPE: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT]`
   - If the format needs a user-supplied hint (e.g. clock frequency): add an
     `IntegerField`/`StringField` to both `ArtefactUploadForm` and `AnalyseForm`,
     populate hints in the upload and analyse view POST handlers, and expose the
     field in the upload/analyse templates (conditionally on artefact type).

3. **`worker/arcworker/tools/flux.py`** — implement `newtype_to_scp_<tool>()`
   returning the standard result dict (`success`, `tool`, `output_path`,
   `output_type=ArtefactType.SCP.value`, `summary`, `process_output`).
   Use `dfi_to_scp_hxcfe()` (hxcfe) or an analogous greaseweazle call as
   the model.  For A2R: `gw convert input.a2r output.scp`.

4. **`worker/arcworker/tools/__init__.py`** — add the new function to the
   `from .flux import …` block and to `__all__`.

5. **`worker/arcworker/analysis.py`**
   - Import the new conversion function at the top.
   - Add `ArtefactType.NEWTYPE` to `_SCP_VIA_CONVERSION_TYPES` (the frozenset
     near the top of the file).  This automatically gates the gw steps (format
     detection, greaseweazle conversion) out of FLUX_DECODE for the new type.
   - **`process_flux_visualisation()`**: inside the
     `if source_type in _SCP_VIA_CONVERSION_TYPES:` block, add an `elif`
     branch that calls the new conversion function.  (DFI is the first branch;
     subsequent formats are `elif source_type == ArtefactType.NEWTYPE:`.)
   - **`process_flux_decode()`**: add `elif source_type == ArtefactType.NEWTYPE:`
     before the `else: (IMD)` fallback.  Call the conversion function, append
     the result to `results`, and register the SCP sibling via
     `self.api.register_derived_artefact()` with **no** `skip_analyses` argument
     so the SCP's own FLUX_DECODE runs the full downstream pipeline.
   - **`_PROMOTABLE_EXTENSIONS`**: add `'.newtype': ArtefactType.NEWTYPE`.

6. **Migration** — write a hand-crafted migration (filename
   `YYYYMMDD_HHMM_add_newtype_artefact_type.py`, revision ID from
   `python3 -c "import time; print(hex(int(time.time()))[2:].zfill(12))"`):
   ```python
   autocommit = True
   def upgrade():
       bind = op.get_bind()
       if bind.dialect.name == 'postgresql':
           op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'NEWTYPE'"))
   ```
   `down_revision` should point at the current head.

7. **Tests** — add a `TestNEWTYPESource` class in `ci/test_flux_decode.py`
   mirroring `TestDFISource`:  verify the conversion tool is called, the SCP
   sibling is registered without `skip_analyses`, and gw/IMD/HFE tools are
   **not** called during the format's own FLUX_DECODE run.

**A2R notes** (implemented — see the worked example commit above):
- Conversion: `gw convert input.a2r output.scp` — greaseweazle handles A2R
  natively, no script or clock-override mechanism needed.
- No hint parameters required (greaseweazle auto-detects the clock).
- CLI `arco upload` already supports generic `--hint KEY=VALUE`; no new fields
  needed for A2R.

### Adding a new archive format

1. Add the new type to `ArchiveType` enum in `shared/archive_formats.py`
2. Add entry to `ARCHIVE_FORMATS` in the same file
3. Add extraction branch in `process_archive_extract` in `worker/arcworker/analysis.py`
4. Update format table in `doc/ARCHIVE_EXTRACTION.md`

### Database changes

1. Edit models in `myapp/database.py`
2. Run `flask db migrate -m "Description"`
3. **Review the generated migration** - Alembic cannot detect renames (shows drop+create), enum changes, or some constraint changes
4. Run `flask db upgrade`

#### Adding values to a PostgreSQL enum

`ALTER TYPE ... ADD VALUE` **cannot run inside a transaction** in PostgreSQL.

`env.py` uses `transaction_per_migration=True`, so each migration runs in its
own transaction. Migrations that need non-transactional DDL (like enum value
additions) can opt out by setting `autocommit = True` at module level:

```python
# At module level, after depends_on:
autocommit = True

def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'NEW_TYPE_NAME'"))
```

> **Case warning:** SQLAlchemy stores enum members by their `.name` (uppercase Python
> identifier), not their `.value`. The DB enum must contain `'FILE_EXTRACTION'` not
> `'file_extraction'`. Always use the UPPERCASE enum name in `ADD VALUE` migrations.

If a migration was already stamped but the enum was never actually updated,
stamp back to the previous revision and re-run:

```bash
flask db stamp <previous_revision_id>
flask db upgrade
```

#### Migration revision IDs

**Never use placeholder or patterned IDs** (e.g. `a1b2c3d4e5f6`, `d1e2f3a4b5c6`).
These look unique but collide with other migrations using the same pattern.

When writing a migration by hand, generate the revision ID from the current UTC
time in seconds, converted to hex, zero-padded to 12 characters:

```python
import time
hex(int(time.time()))[2:].zfill(12)   # e.g. '000069b41ab4'
```

Or from the shell:

```bash
python3 -c "import time; print(hex(int(time.time()))[2:].zfill(12))"
```

For Alembic-generated migrations (`flask db migrate`), keep the generated
`revision` unchanged. The guidance above only applies to migrations written by
hand.

#### Migration filename convention

Migration files must be named `YYYYMMDD_HHMMSS_description.py` (UTC), e.g.
`20260404_001750_add_riscos_load_exec_address.py`.

The timestamp prefix is an **ordering key**, not a filesystem metadata dump.
Directory listing order must match the Alembic `down_revision` chain so the
last listed file is the current head. CI checks this in
`ci/check_migration_sanity.py`.

Rules:

- For a new migration, use the current UTC timestamp.
- If a migration is rebased or renamed to resolve merge-head ordering, rename
  the file so lexicographic filename order matches migration-chain order.
- Do **not** use filesystem `mtime` or the header `Create Date` as the
  ordering source.
- Keep the filename timestamp aligned with the hex `revision` timestamp where
  practical.
- Do **not** use the raw hex revision ID as the filename prefix.

#### Collapsing migrations before merge

When a feature branch accumulates several migrations that are purely development
artefacts (rename steps, incremental fixes that could have been in the original
schema), collapse them into one before merging so the project history stays clean.

Rules:
- **Keep the final (head) `revision` ID.** Any environment already stamped at the
  head remains compatible without manual intervention.
- **Set `down_revision` to the pre-branch head** — the last revision that existed
  on the target branch before your first migration.
- Delete the intermediate migration files; they must not remain in the tree.
- The consolidated migration's filename timestamp should match the final revision's
  original timestamp so lexicographic ordering is preserved.

If migrations are intentionally kept separate (e.g. one adds a table, another adds
an index that deserves its own entry for clarity), apply the same revision-ID rules
to each file in the chain — each file's `revision` stays unchanged, only
`down_revision` of the *first* file in the chain is adjusted if the base moved.

### RISC OS INF sidecar file processing

Several BBC Micro and RISC OS tools (currently Disc Image Manager; others may
follow) produce `.inf` sidecar files alongside extracted files.  These contain
metadata that Unix filesystems cannot represent: load address, exec address,
RISC OS filetype, file attributes, and the original BBC filename.

`process_inf_sidecars(output_dir)` in `worker/arcworker/tools/extraction.py` is
the reusable pre-processing step.  It must be called **before**
`enumerate_extracted_files()` so that files are renamed and metadata is
collected before hashing and enumeration.  It returns a `dict[str, dict]`
mapping display paths to metadata, which is passed as the `inf_metadata`
parameter to `enumerate_extracted_files()`.

Each extraction tool that produces INF files is responsible for calling
`process_inf_sidecars()` before it returns and including the result in its
return dict as `'inf_metadata'`.  The caller passes this through to
`enumerate_extracted_files()`.  Currently only
`extract_acorn_disc_image_manager()` calls it.

#### How it works

1. Walks `output_dir` for files with `.inf` extension (case-insensitive).
2. Validates each INF has a matching data file (same path minus `.inf`).
3. Parses INF contents: `<filename> <load_hex> <exec_hex> [<length_hex>] [<access>]`.
4. Derives RISC OS filetype from load address when date-stamped (top 12 bits = `0xFFF`).
5. Renames data file from DOS-encoded name to original BBC name if needed.
6. Deletes the INF file.
7. Returns metadata dict for `enumerate_extracted_files()` to merge into file records.

#### BBC ↔ DOS filename character translation

Files stored on a DOS/Windows host filesystem use safe substitution characters.
The INF records the original BBC character.  On Linux all BBC characters are
valid, so files are renamed back to the BBC originals.

```
BBC    DOS
 #  ↔  ?
 .  ↔  /
 $  ↔  <
 ^  ↔  >
 &  ↔  +
 @  ↔  =
 %  ↔  ;
```

The translation table is defined as `_BBC_TO_DOS` / `_DOS_TO_BBC` in
`extraction.py`.  To add or change mappings, edit both dicts (they are
inverses of each other).

> **Note:** The `.` ↔ `/` mapping means these characters cannot appear in
> filenames on DOS/Windows hosts (since `/` is a path separator).  On Linux
> both are valid filename characters, so the rename works correctly.

#### Extending to other archive processors

To add INF support to a new extraction tool:

1. Ensure the tool writes standard `.inf` sidecar files alongside extracted
   data files (one `.inf` per file, same base name).
2. Call `process_inf_sidecars(output_dir)` inside the tool's extraction
   wrapper (before returning) and include the result in the return dict as
   `'inf_metadata'`.  See `extract_acorn_disc_image_manager()` for the
   pattern.
3. The caller must pass the `'inf_metadata'` dict through to
   `enumerate_extracted_files()` via its `inf_metadata` parameter.
4. If the tool uses a **different filename translation table** (e.g. a
   platform-specific mapping), either:
   - Add a `translation_table` parameter to `process_inf_sidecars()`, or
   - Normalise filenames in the tool's extraction wrapper before
     `process_inf_sidecars()` runs (the same approach used by
     `normalize_extracted_filenames()` for RISC OS Latin-1 byte sequences).
5. If the tool's INF format differs from the standard (extra fields, different
   field order), extend `_parse_inf_line()` in `extraction.py`.

#### Database fields populated from INF metadata

| `ExtractedFile` column | Source | Example |
|------------------------|--------|---------|
| `load_address` (String(8)) | INF field 2, zero-padded hex | `'fffffd00'` |
| `exec_address` (String(8)) | INF field 3, zero-padded hex | `'ffffff00'` |
| `risc_os_filetype` (String(3)) | Derived from load address bits 19:8 | `'ffd'` |
| `attributes` (String(50)) | INF field 5, stored as-is | `'WR/r'`, `'L'`, `'33'` |

### Analysis pipeline flow

Upload triggers auto-analysis based on `ANALYSIS_MAP` -> worker claims job atomically -> processes with external tools -> reports results via API -> derived artefacts trigger follow-on analyses (e.g., flux -> decode -> file listing).

## Important Files to Know

| File | Role |
|------|------|
| `shared/enums.py` | `ArtefactType` and `AnalysisType` — single source of truth for web, worker, and CLI |
| `shared/archive_formats.py` | `ArchiveType`, `ARCHIVE_FORMATS`, helpers — single source of truth |
| `shared/storage.py` | Storage backend abstraction (`LocalStorage` and `S3Storage`); selected via `STORAGE_BACKEND` env var |
| `myapp/database.py` | All SQLAlchemy models and web-specific enums (`AnalysisStatus`, `FilesystemType`, etc.) |
| `myapp/blueprints/artefacts.py` | `EXTENSION_MAP` (type detection) and `ANALYSIS_MAP` (auto-analysis rules) |
| `myapp/blueprints/search.py` | Global search: `parse_query()`, `_run_search()`, prefix query syntax |
| `myapp/blueprints/api.py` | REST API consumed by workers and CLI |
| `myapp/riscos_filetypes.py` | RISC OS filetype hex↔name mapping; `lookup_filetype_hex()` |
| `worker/arcworker/analysis.py` | Worker job handlers |
| `worker/arcworker/tools/extraction.py` | File extraction tools, INF sidecar processing, BBC↔DOS filename translation |
| `cli/arccli/main.py` | CLI entry point and argument parsing |
| `cli/arccli/client.py` | CLI HTTP client for the REST API |
| `myapp/app.py` | Application factory, login/error handlers, blueprint registration |
| `myapp/myapp.cfg.example` | Configuration template with all settings |

## Testing

Automated tests live in `ci/` and run in the `app-tests` GitHub Actions job (SQLite in-memory, no PostgreSQL needed):

| Test file | What it covers |
|-----------|---------------|
| `ci/test_app_smoke.py` | App starts, `/api/health`, API key auth |
| `ci/test_search.py` | `parse_query()`, `lookup_filetype_hex()`, `_run_search()` with fixture data (every search key), HTTP endpoint smoke |
| `ci/test_artefact_map.py` | `EXTENSION_MAP` / `ANALYSIS_MAP` consistency |
| `ci/test_archive_formats.py` | Archive format completeness |
| `ci/test_slug.py` | Slug generation (stdlib only, no pip) |
| `ci/test_idempotency.py` | Analysis pipeline idempotency (prevent duplicate Analysis/Artefact rows) |
| `ci/test_checksum_compute.py` | Hash computation |
| `ci/test_url_identifiers.py` | URL-safe identifiers and slug generation |
| `ci/test_fk_violations.py` | FK cascade deletes, defensive checks, M2M cleanup, nullable FK edge cases (SQLite FK enforcement enabled) |
| `ci/test_inf_processing.py` | RISC OS INF sidecar parsing, BBC↔DOS filename translation, `process_inf_sidecars()` end-to-end |

Run locally:

```bash
SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
    python -m unittest discover -s ci -p "test_*.py" -v
```

CI also runs Ruff on every push. Check and auto-fix before committing:

```bash
ruff check myapp/ shared/ worker/ cli/          # report issues
ruff check --fix myapp/ shared/ worker/ cli/    # apply safe fixes
```

When modifying code, also verify manually:
- Web UI operations (CRUD, upload, search)
- API endpoints (worker communication)
- Analysis pipeline (if worker-related changes)
- Database migrations (both upgrade and downgrade)

## Dependencies

Python packages (from `requirements.txt`): Flask, SQLAlchemy, Flask-SQLAlchemy, Flask-Migrate, Flask-Login, Flask-WTF, bootstrap-flask, bcrypt, python-dotenv, requests, psycopg2-binary, watchdog. Note: `simplejson` is listed in requirements.txt but currently unused.

Worker external tools (compiled in worker Dockerfile): Fluxfox (Rust), HxCFE (C), Greaseweazle (Python), DiscImageManager (Lazarus/Pascal), 7z, fcfs2raw (C utility in `worker/tools/`).

## Common Gotchas

- **Switching branches with migrations**: The database schema is independent of your git branch. Before checking out a branch that has different migrations, run `python devtools/db_branch_switch.py [target-branch]` to downgrade the DB first. See `doc/BRANCH_DB_SWITCHING.md`.
- `ArtefactType` and `AnalysisType` live in `shared/enums.py` — edit there only; web app, worker, and CLI all import from it
- `ArchiveType` and `ARCHIVE_FORMATS` live in `shared/archive_formats.py` — edit there only
- When running the worker **outside Docker** locally, run from the repo root: `python worker/worker.py`. The entry point adds the repo root to `sys.path` automatically so `shared/` is importable
- The worker Dockerfile multi-stage build compiles several tools from source and is slow to build
- `myapp.cfg` is optional — environment variables take precedence and suffice for Docker deployments. `SQLALCHEMY_DATABASE_URI`, `SECRET_KEY`, and `WORKER_API_KEY` are all read from the environment if not set in `myapp.cfg`
- `SECRET_KEY` auto-generates with a warning if missing, left at the default placeholder, or too short — set it explicitly in `.env` or `myapp.cfg` for persistent sessions
- Alembic auto-generated migrations need manual review for renames and enum changes
- **PostgreSQL enum pitfall**: `ALTER TYPE ... ADD VALUE` cannot run inside a transaction — set `autocommit = True` at module level. Also, downgrade() **must** delete/remap rows using the new value, or a branch switch leaves orphan rows that crash the ORM with `LookupError`. CI enforces this. See "Adding values to a PostgreSQL enum" above for the exact patterns.
- Docker entrypoint (`Dentrypoint.sh`) runs `flask db upgrade` and `flask create-admin` on every start (both are idempotent)
- `flask create-admin` reads `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars non-interactively; prompts if a TTY is available; warns and exits cleanly if neither. Passwords must be at least 12 characters
- **Do NOT use Python's `zipfile` module on RISC OS ZIPs.** RISC OS ZIP archives contain Acorn-specific extra-field blocks (ID `0x4341` / "AC") that `zipfile.ZipFile` rejects with `BadZipFile`. Any code that needs to read RISC OS ZIP metadata (filenames, structure) must parse the ZIP central directory manually with `struct`, or shell out to `unzip`. The worker's `_is_riscos_zip()` and `extract_zip_riscos()` both avoid `zipfile` for this reason.
- **Migration branch conflicts**: If two PRs both add a migration extending the same chain head, merging both creates "Multiple head revisions." CI detects this on PRs via `ci/check_migration_conflict.py`. A pre-push hook is available in `hooks/` (`git config core.hooksPath hooks`). See `doc/MIGRATION_CONFLICTS.md` for resolution steps.
- Upload limit is 4GB (`MAX_CONTENT_LENGTH` in config)
- `STALE_JOB_TIMEOUT_SECONDS` (default 3600) controls how long a job may stay in `RUNNING` state before it is considered stuck and eligible for reset back to `PENDING`. Set this above the longest expected analysis run time.
- **S3 object Content-Type must be set at upload time, not inferred at read.** S3-compatible backends (Garage, MinIO, AWS) do not auto-detect MIME types from the key extension — if the object is uploaded without `ContentType`, browsers receive a wrong/generic type (often `text/xml` or `application/octet-stream`) and download instead of rendering inline. This was first hit with `.svg` outputs from Draw-to-SVG (fixed in commit `44091c1`) and applies equally to any worker-generated output (PNG, JPEG, SVG from WMF/EMF, etc.). `shared/storage.py`'s `S3Storage.put()` / `put_tree()` already handle this by calling `mimetypes.guess_type()` and passing `ExtraArgs={'ContentType': ...}`; `presigned_url()` also sets `ResponseContentType`. When adding a new output format, make sure (a) the saved filename has the correct extension, and (b) `mimetypes` can map that extension — for unusual types, call `mimetypes.add_type(...)` at module load in `shared/storage.py` (as we do for `.svg`). On the read side, `get_output_file()` in `myapp/blueprints/artefacts.py` and `myapp/blueprints/api.py` must also pass `mimetype=` to `send_file()` so local-storage serving matches.
- **Bootstrap 5 collapse + `stopPropagation` does not work reliably.** Bootstrap 5's collapse plugin wires its click listener via document-level event delegation, so calling `event.stopPropagation()` on a child element (or even on the toggle element's immediate container) does not prevent the collapse from firing. The reliable fix is to remove `data-bs-toggle`/`data-bs-target` from the toggle element and drive the collapse programmatically: add a JS `click` listener on the row, check `event.target.closest('.some-actions-class')` to bail out early for clicks on interactive children, and call `bootstrap.Collapse.getOrCreateInstance(collapseEl, { toggle: false }).toggle()` for all other clicks. See `myapp/templates/hashdb/view.html` for a worked example.
