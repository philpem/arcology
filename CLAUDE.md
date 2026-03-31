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

### Debug tools

- `devtools/run_debug.py` - Flask debug mode with auto-reload
- `DEBUG_DB_LOG = True` in `myapp.cfg` - Log all SQL queries
- `DEBUG_DB_PROFILING = True` in `myapp.cfg` - SQL profiling reports (requires `sqltap`)

## Code Style and Conventions

- **Indentation**: 4 spaces per level (PEP 8 standard). Files end with `# vim: ts=4 sw=4 et`.
- **Python version**: 3.10+ (uses PEP 585 type hints in newer code)
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

The same applies to `ArtefactType` and any other SQLAlchemy `Enum` column
backed by a Python `enum.Enum` class in this project.

### Adding a new artefact type

1. Add to `ArtefactType` enum in `shared/enums.py`
2. Add extension mapping in `EXTENSION_MAP` in `myapp/blueprints/artefacts.py`
3. Add entries to `ANALYSIS_MAP` for auto-queued analyses

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

Alembic-generated migrations (`flask db migrate`) produce their own random IDs
automatically — this only applies to hand-written migrations.

### Analysis pipeline flow

Upload triggers auto-analysis based on `ANALYSIS_MAP` -> worker claims job atomically -> processes with external tools -> reports results via API -> derived artefacts trigger follow-on analyses (e.g., flux -> decode -> file listing).

## Important Files to Know

| File | Role |
|------|------|
| `shared/enums.py` | `ArtefactType` and `AnalysisType` — single source of truth for web, worker, and CLI |
| `shared/archive_formats.py` | `ArchiveType`, `ARCHIVE_FORMATS`, helpers — single source of truth |
| `myapp/database.py` | All SQLAlchemy models and web-specific enums (`AnalysisStatus`, `FilesystemType`, etc.) |
| `myapp/blueprints/artefacts.py` | `EXTENSION_MAP` (type detection) and `ANALYSIS_MAP` (auto-analysis rules) |
| `myapp/blueprints/search.py` | Global search: `parse_query()`, `_run_search()`, prefix query syntax |
| `myapp/blueprints/api.py` | REST API consumed by workers and CLI |
| `myapp/riscos_filetypes.py` | RISC OS filetype hex↔name mapping; `lookup_filetype_hex()` |
| `worker/arcworker/analysis.py` | Worker job handlers |
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

Run locally:

```bash
SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
    python -m unittest discover -s ci -p "test_*.py" -v
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

- `ArtefactType` and `AnalysisType` live in `shared/enums.py` — edit there only; web app, worker, and CLI all import from it
- `ArchiveType` and `ARCHIVE_FORMATS` live in `shared/archive_formats.py` — edit there only
- When running the worker **outside Docker** locally, run from the repo root: `python worker/worker.py`. The entry point adds the repo root to `sys.path` automatically so `shared/` is importable
- The worker Dockerfile multi-stage build compiles several tools from source and is slow to build
- `myapp.cfg` is optional — environment variables take precedence and suffice for Docker deployments. `SQLALCHEMY_DATABASE_URI`, `SECRET_KEY`, and `WORKER_API_KEY` are all read from the environment if not set in `myapp.cfg`
- `SECRET_KEY` auto-generates with a warning if missing, left at the default placeholder, or too short — set it explicitly in `.env` or `myapp.cfg` for persistent sessions
- Alembic auto-generated migrations need manual review for renames and enum changes
- **PostgreSQL enum pitfall**: `ALTER TYPE ... ADD VALUE` cannot run inside a transaction. Set `autocommit = True` at module level in migrations that add enum values (see "Adding values to a PostgreSQL enum" above)
- Docker entrypoint (`Dentrypoint.sh`) runs `flask db upgrade` and `flask create-admin` on every start (both are idempotent)
- `flask create-admin` reads `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars non-interactively; prompts if a TTY is available; warns and exits cleanly if neither. Passwords must be at least 12 characters
- **Do NOT use Python's `zipfile` module on RISC OS ZIPs.** RISC OS ZIP archives contain Acorn-specific extra-field blocks (ID `0x4341` / "AC") that `zipfile.ZipFile` rejects with `BadZipFile`. Any code that needs to read RISC OS ZIP metadata (filenames, structure) must parse the ZIP central directory manually with `struct`, or shell out to `unzip`. The worker's `_is_riscos_zip()` and `extract_zip_riscos()` both avoid `zipfile` for this reason.
- Upload limit is 4GB (`MAX_CONTENT_LENGTH` in config)
