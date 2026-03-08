# CLAUDE.md - Arcology Development Guide

## Project Overview

Arcology is a digital artefact catalogue for retrocomputing collections, built on Flask. It enables cataloguing, uploading, and automatic analysis of digital artifacts like disk images, flux dumps, and archives from historical computer media.

The system has two main components connected via a REST API:
- **Web application** (`myapp/`) - Flask app serving the UI and REST API
- **Analysis worker** (`worker/`) - Standalone Python process that polls for analysis jobs and runs external tools

## Architecture

```
Web (Flask)  <-- HTTP/JSON -->  Worker (Python)
     |                              |
     | SQLAlchemy                   | Shared volumes
     v                              v
PostgreSQL                    uploads/ & outputs/
```

- The worker has **no direct database access** - all communication goes through the REST API
- Shared filesystem volumes: `data/uploads/` (originals) and `data/outputs/` (analysis results)
- Workers claim jobs atomically via `PUT /api/analysis/{id}` to prevent duplicate processing

## Repository Structure

```
arcology/
├── myapp/                      # Flask web application
│   ├── app.py                  # Application factory (create_app)
│   ├── database.py             # All SQLAlchemy models and enums (single source of truth)
│   ├── extensions.py           # Flask extension instances (db, migrate, login_manager, bootstrap, csrf)
│   ├── __main__.py             # Dev server entry point (python -m myapp)
│   ├── myapp.cfg.example       # Config template
│   ├── archive_formats.py      # Archive type definitions (must match worker copy)
│   ├── riscos_filetypes.py     # RISC OS filetype mapping
│   ├── blueprints/             # Feature modules (auto-discovered)
│   │   ├── dashboard.py        # Homepage with collection stats
│   │   ├── items.py            # Item CRUD (search, filter, pagination)
│   │   ├── artefacts.py        # File upload, type detection, ANALYSIS_MAP
│   │   ├── taxonomy.py         # Platforms, categories, tags, external systems
│   │   ├── analysis.py         # Analysis queue UI
│   │   └── api.py              # REST API for workers and external tools
│   ├── utils/                  # Utility modules
│   ├── templates/              # Jinja2 templates (Bootstrap 5)
│   └── static/                 # CSS
├── worker/                     # Analysis worker (separate container)
│   ├── worker.py               # Entry point
│   ├── Dockerfile              # Multi-stage build compiling HxCFE, Fluxfox, etc.
│   └── arcworker/              # Worker package
│       ├── analysis.py         # AnalysisWorker class and job handlers
│       ├── api.py              # HTTP client for web API
│       ├── types.py            # Enum copies (must match database.py)
│       ├── archive_formats.py  # Archive format definitions (must match web copy)
│       ├── config.py           # Environment-based config
│       ├── compression.py      # Decompression utilities
│       └── tools/              # Wrappers for external analysis tools
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

- **Indentation**: Tabs (not spaces). Files end with `# vim: ts=4 sw=4 noet`.
- **Python version**: 3.10+ (uses PEP 585 type hints in newer code)
- **UUIDs for public identifiers**: URLs and API responses use UUID hex strings, not sequential integer IDs
- **Application factory pattern**: `create_app()` in `app.py`; extensions bound in factory, not at import time
- **Blueprint auto-discovery**: Any module in `myapp/blueprints/` with a `blueprint` variable is auto-registered. Optional `init_app(app)` for additional setup.
- **Single database model file**: All models and enums live in `myapp/database.py` (source of truth)
- **Enum duplication**: Worker has its own copy of enums in `worker/arcworker/types.py` - these must be kept in sync with `database.py`
- **Archive format duplication**: `myapp/archive_formats.py` and `worker/arcworker/archive_formats.py` must be kept in sync
- **CSRF**: Enabled globally via Flask-WTF. The API blueprint exempts itself in `init_app()`.
- **Security**: bcrypt password hashing, CSRF protection, UUID-based URLs (no IDOR)

## Key Patterns

### Adding a new blueprint

1. Create `myapp/blueprints/yourfeature.py`
2. Define a `blueprint` variable (Flask Blueprint instance)
3. It will be auto-discovered and registered by `_register_blueprints()` in `app.py`

### Adding a new analysis type

1. Add to `AnalysisType` enum in `myapp/database.py`
2. Add to `ANALYSIS_MAP` in `myapp/blueprints/artefacts.py`
3. Implement handler in `worker/arcworker/analysis.py`
4. Add enum to `worker/arcworker/types.py`

### Adding a new artefact type

1. Add to `ArtefactType` enum in `myapp/database.py`
2. Add extension mapping in `EXTENSION_MAP` in `myapp/blueprints/artefacts.py`
3. Add entries to `ANALYSIS_MAP` for auto-queued analyses
4. Add enum to `worker/arcworker/types.py`

### Adding a new archive format

1. Add the new type to `ArchiveType` enum in **both** `myapp/archive_formats.py` and `worker/arcworker/archive_formats.py`
2. Add entry to `ARCHIVE_FORMATS` in **both** copies
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
        op.execute(sa.text("ALTER TYPE myenum ADD VALUE IF NOT EXISTS 'NEW_VALUE'"))
```

If a migration was already stamped but the enum was never actually updated,
stamp back to the previous revision and re-run:

```bash
flask db stamp <previous_revision_id>
flask db upgrade
```

### Analysis pipeline flow

Upload triggers auto-analysis based on `ANALYSIS_MAP` -> worker claims job atomically -> processes with external tools -> reports results via API -> derived artefacts trigger follow-on analyses (e.g., flux -> decode -> file listing).

## Important Files to Know

| File | Role |
|------|------|
| `myapp/database.py` | All models and enums - schema source of truth |
| `myapp/blueprints/artefacts.py` | `EXTENSION_MAP` (type detection) and `ANALYSIS_MAP` (auto-analysis rules) |
| `myapp/blueprints/api.py` | REST API consumed by workers |
| `myapp/archive_formats.py` | Archive type definitions (must match worker copy) |
| `worker/arcworker/analysis.py` | Worker job handlers |
| `worker/arcworker/types.py` | Worker-side enum copies (must match `database.py`) |
| `worker/arcworker/archive_formats.py` | Worker-side archive format definitions (must match web copy) |
| `myapp/app.py` | Application factory, login/error handlers, blueprint registration |
| `myapp/myapp.cfg.example` | Configuration template with all settings |

## Testing

There is no automated test suite. Changes are tested manually via the web UI and Docker Compose. When modifying code, verify:
- Web UI operations (CRUD, upload, search)
- API endpoints (worker communication)
- Analysis pipeline (if worker-related changes)
- Database migrations (both upgrade and downgrade)

## Dependencies

Python packages (from `requirements.txt`): Flask, SQLAlchemy, Flask-SQLAlchemy, Flask-Migrate, Flask-Login, Flask-WTF, bootstrap-flask, bcrypt, python-dotenv, requests, psycopg2-binary, watchdog. Note: `simplejson` is listed in requirements.txt but currently unused.

Worker external tools (compiled in worker Dockerfile): Fluxfox (Rust), HxCFE (C), Greaseweazle (Python), DiscImageManager (Lazarus/Pascal), 7z, fcfs2raw (C utility in `worker/tools/`).

## Common Gotchas

- Worker enums in `types.py` must match web app enums in `database.py` - they are separate copies
- Archive format definitions in `myapp/archive_formats.py` and `worker/arcworker/archive_formats.py` must also be kept in sync
- The worker Dockerfile multi-stage build compiles several tools from source and is slow to build
- `myapp.cfg` is optional — environment variables take precedence and suffice for Docker deployments. `SQLALCHEMY_DATABASE_URI`, `SECRET_KEY`, and `WORKER_API_KEY` are all read from the environment if not set in `myapp.cfg`
- `SECRET_KEY` auto-generates with a warning if missing, left at the default placeholder, or too short — set it explicitly in `.env` or `myapp.cfg` for persistent sessions
- Alembic auto-generated migrations need manual review for renames and enum changes
- **PostgreSQL enum pitfall**: `ALTER TYPE ... ADD VALUE` cannot run inside a transaction. Set `autocommit = True` at module level in migrations that add enum values (see "Adding values to a PostgreSQL enum" above)
- Docker entrypoint (`Dentrypoint.sh`) runs `flask db upgrade` and `flask create-admin` on every start (both are idempotent)
- `flask create-admin` reads `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars non-interactively; prompts if a TTY is available; warns and exits cleanly if neither. Passwords must be at least 12 characters
- Upload limit is 4GB (`MAX_CONTENT_LENGTH` in config)
