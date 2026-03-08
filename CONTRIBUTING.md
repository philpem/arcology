# Contributing to Arcology

Thanks for your interest in contributing to Arcology! This guide will help you get oriented with the codebase and start making changes.

## Architecture Overview

Arcology is a digital artefact catalogue for retrocomputing collections. It has two main components -- a **web application** and an **analysis worker** -- connected via a REST API, with PostgreSQL as the backing database.

```
┌─────────────────────┐         ┌─────────────────────┐
│     Web (Flask)      │         │   Worker (Python)    │
│                      │  HTTP   │                      │
│  - UI (Bootstrap 5)  │◄───────►│  - Polls for jobs    │
│  - REST API          │  JSON   │  - Runs analysis     │
│  - Auth / CRUD       │         │  - Returns results   │
│  - Upload handling   │         │  - Produces artefacts│
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           │ SQLAlchemy                      │ Shared volumes
           ▼                                ▼
┌─────────────────────┐         ┌─────────────────────┐
│   PostgreSQL         │         │  uploads/ & outputs/ │
│   (data store)       │         │  (file storage)      │
└─────────────────────┘         └─────────────────────┘
```

### Web Application (`myapp/`)

The web app is a Flask application using the application factory pattern. It serves the browser UI and exposes the REST API that workers call.

**Key files:**

- `myapp/app.py` -- Application factory (`create_app()`). Creates the Flask app, initialises extensions, registers blueprints, and sets up login/error handlers.
- `myapp/extensions.py` -- Flask extension instances (SQLAlchemy, Migrate, LoginManager, Bootstrap5, CSRF). Created without being bound to an app; initialised later in the factory.
- `myapp/database.py` -- All SQLAlchemy models and the web-specific enums (`AnalysisStatus`, `FilesystemType`, etc.). `ArtefactType` and `AnalysisType` are imported from `shared/enums.py`.
- `myapp/myapp.cfg` -- Runtime configuration (database URI, secret key, upload paths, etc.). Copied from `myapp.cfg.example`.

**Blueprints** (`myapp/blueprints/`) -- each feature area is a separate Flask blueprint:

| Blueprint | Purpose |
|-----------|---------|
| `dashboard.py` | Homepage with collection statistics |
| `items.py` | CRUD for catalogue items (search, filter, pagination) |
| `artefacts.py` | File upload, type detection, artefact management. Also contains the `ANALYSIS_MAP` that determines which analyses are auto-queued for each artefact type. |
| `taxonomy.py` | Platforms, categories, tags, external systems, hash databases |
| `analysis.py` | Analysis queue UI (view, cancel, retry jobs) |
| `api.py` | REST API endpoints consumed by workers and external tools |

Blueprints are auto-discovered and registered -- any module in `myapp/blueprints/` that defines a `blueprint` variable will be loaded automatically. Modules can also provide an `init_app(app)` function for additional setup (e.g., the API blueprint uses this to exempt itself from CSRF).

**Templates** are in `myapp/templates/` (Jinja2) and **static assets** in `myapp/static/` (CSS/JS).

### Analysis Worker (`worker/`)

The worker is a standalone Python process that polls the web app's REST API for pending analysis jobs, processes them using external command-line tools, and reports results back.

**Key files:**

- `worker/worker.py` -- Entry point. Reads config from environment variables and starts the worker loop.
- `worker/arcworker/analysis.py` -- `AnalysisWorker` class. Contains the main poll loop and all analysis handler methods (one per analysis type).
- `worker/arcworker/api.py` -- `ArcologyAPI` class. HTTP client that talks to the web app's REST API.
- `worker/arcworker/config.py` -- Configuration from environment variables.
- `worker/arcworker/compression.py` -- Decompression utilities for compressed artefacts.
- `worker/arcworker/tools/` -- Wrappers for external analysis tools (HxCFE, Fluxfox, 7z, etc.).
- `worker/Dockerfile` -- Multi-stage build that compiles all analysis tools from source.
- `shared/enums.py` -- Canonical `ArtefactType` and `AnalysisType` enum definitions, imported by both web app and worker.
- `shared/archive_formats.py` -- Canonical archive format definitions (`ArchiveType`, `ARCHIVE_FORMATS`, helpers), imported by the worker.

**How workers process jobs:**

1. The worker polls `GET /api/analysis/pending` on a configurable interval (default 30s).
2. It attempts to **claim** a job atomically via `PUT /api/analysis/{id}` with `claim_worker: true`. The server uses an atomic `UPDATE ... WHERE status = 'pending'` query so only one worker can claim each job, even with multiple workers running.
3. A temporary working directory is created for the job.
4. The appropriate handler method runs external tools and processes the artefact.
5. Results are reported back to the API. Handlers can:
   - Update analysis status (completed/failed with details)
   - Register **derived artefacts** (e.g., a decoded IMG produced from a flux image)
   - Register **file listings** (directory contents extracted from a disk image)
6. Derived artefacts automatically trigger follow-on analyses based on the `ANALYSIS_MAP` in `artefacts.py`, creating an analysis chain.

**Analysis types and the tools they use:**

| Analysis Type | Tools | What it Does |
|---------------|-------|-------------|
| `FLUX_VISUALISATION` | Fluxfox, HxCFE | Generates graphical plots of magnetic flux data |
| `FLUX_DECODE` | HxCFE, Greaseweazle | Converts flux images to sector formats (IMD, HFE, IMG) |
| `FILE_EXTRACTION` | 7z, DiscImageManager | Extracts files from disk images and registers file listing |
| `METADATA_EXTRACT` | (built-in) | Computes hashes and extracts format metadata |
| `PARTITION_DETECT` | sfdisk, ADFS signature detection, `file` | Detects partitions and filesystem types |
| `FORMAT_IDENTIFY` | (placeholder) | Identifies exact format/variant |

### How Web and Worker Communicate

The web and worker components communicate exclusively through the REST API (JSON over HTTP). They also share two filesystem directories via Docker volumes:

- `data/uploads/` -- Original user-uploaded files. The web app writes here; workers read from here.
- `data/outputs/` -- Analysis outputs (visualisations, derived artefacts). Workers write here; the web app serves files from here.

The worker has no direct database access. All data flows through the API.

### Database

PostgreSQL is the primary database (SQLite is supported for development). The schema is defined in `myapp/database.py` using SQLAlchemy models. Key entities:

- **Item** -- A logical catalogue entry (e.g., "WordStar 3.0 for CP/M").
- **Artefact** -- A single digital file attached to an item (e.g., a KryoFlux dump). Artefacts can be derived from other artefacts via analysis, forming a tree.
- **Analysis** -- A job record with status tracking (pending/running/completed/failed).
- **Partition / ExtractedFile** -- File listings extracted from disk images.
- **Platform / Category / Tag** -- Hierarchical taxonomy for organising items.
- **ExternalSystem / ExternalReference** -- Links to external cataloguing systems.
- **HashDatabase / KnownFile** -- Known file hashes for identifying common files.

Migrations are managed with Flask-Migrate (Alembic).

## Development Setup

### Prerequisites

- Python 3.10+
- PostgreSQL (or SQLite for quick local development)
- Docker and Docker Compose (for running the full stack including workers)

### Local Development (Web Only)

```bash
# Clone the repository
git clone <repo-url>
cd arcology

# Create a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp myapp/myapp.cfg.example myapp/myapp.cfg
# Edit myapp/myapp.cfg:
#   - Set SECRET_KEY (or leave the default for dev -- it auto-generates one)
#   - Set SQLALCHEMY_DATABASE_URI for your database

# Apply database migrations
flask db upgrade

# Create an admin user (interactive -- prompts for username and password)
flask create-admin

# Run the development server
python -m myapp
# Visit http://localhost:5000
```

Note: without a worker running, uploaded artefacts won't be analysed. The web UI will still work for cataloguing.

### Full Stack with Docker

```bash
# Create data directories
mkdir -p data/uploads data/outputs data/db

# Build and start everything (first build compiles analysis tools -- this is slow)
docker compose up --build -d

# Start with multiple workers for parallel analysis
docker compose up -d --scale worker=4

# View logs
docker compose logs -f web
docker compose logs -f worker

# Access the web UI at http://localhost:8000
# For database browsing with Adminer, see docker-compose.adminer.yml

# Rebuild after code changes
docker compose up --build --force-recreate -d

# Stop
docker compose down
```

### Running the Worker Outside Docker

The worker uses a `shared/` package that lives in the repo root. When running
the worker locally (outside Docker) you must ensure the repo root is on the
Python path. The entry point (`worker/worker.py`) handles this automatically,
but you must run it from the **repo root** or use `PYTHONPATH`:

```bash
# From the repo root (recommended):
python worker/worker.py

# Or with an explicit PYTHONPATH if running from another directory:
PYTHONPATH=/path/to/arcology python worker/worker.py
```

Inside Docker the `shared/` directory is copied into the container at build
time, so no special path setup is needed.

### Debug Tools

- `devtools/run_debug.py` -- Runs Flask in debug mode with auto-reload.
- `DEBUG_DB_LOG = True` in `myapp.cfg` -- Logs all SQL queries.
- `DEBUG_DB_PROFILING = True` in `myapp.cfg` -- Generates SQL profiling reports (requires the `sqltap` package).

## Making Changes

### Project Layout Reference

```
arcology/
├── myapp/                      # Web application
│   ├── app.py                  # Application factory
│   ├── database.py             # All models and web-specific enums
│   ├── extensions.py           # Flask extension instances
│   ├── myapp.cfg.example       # Config template
│   ├── blueprints/             # Feature modules (auto-discovered)
│   ├── templates/              # Jinja2 HTML templates
│   └── static/                 # CSS, JS, images
├── shared/                     # Shared definitions (used by web app and worker)
│   ├── enums.py                # ArtefactType and AnalysisType (single source of truth)
│   └── archive_formats.py      # Archive format definitions
├── worker/                     # Analysis worker
│   ├── worker.py               # Entry point
│   ├── Dockerfile              # Multi-stage build with analysis tools
│   └── arcworker/              # Worker package
│       ├── analysis.py         # Job processing and handlers
│       ├── api.py              # REST API client
│       ├── config.py           # Environment-based config
│       ├── compression.py      # Decompression utilities
│       └── tools/              # External tool wrappers
├── docker-compose.yml          # Full stack orchestration
├── Dockerfile                  # Web container
├── Dentrypoint.sh              # Web container startup script
├── requirements.txt            # Python dependencies
└── doc/                        # Additional documentation
```

### Adding a New Blueprint

1. Create a new file in `myapp/blueprints/`, e.g. `myapp/blueprints/reports.py`.
2. Define a Flask `Blueprint` object named `blueprint`.
3. The application factory will auto-discover and register it.
4. Optionally define `init_app(app)` for any app-level setup.

### Adding a New Analysis Type

1. Add the new type to `AnalysisType` in `shared/enums.py`.
2. Add it to the `ANALYSIS_MAP` in `myapp/blueprints/artefacts.py` so it gets auto-queued for the appropriate artefact types.
3. Implement a `process_<type>` handler method in `worker/arcworker/analysis.py`.
4. Register the handler in the `handlers` dict inside `AnalysisWorker.process_analysis()`.

### Adding a New Artefact Type

1. Add the type to `ArtefactType` in `shared/enums.py`.
2. Update the file extension detection logic in `myapp/blueprints/artefacts.py`.
3. Add entries to `ANALYSIS_MAP` to specify which analyses should auto-run.

### Adding a New Archive Format

Archive format definitions live in `shared/archive_formats.py`.

1. Add the new type to the `ArchiveType` enum in `shared/archive_formats.py`.
2. Add an entry to `ARCHIVE_FORMATS` in the same file, including:
   - `name` -- display string shown in the GUI (e.g. `"ZIP (RISC OS)"`)
   - `category` -- `ArchiveCategory.ARCHIVE`, `.COMPRESS`, or `.DISK_IMAGE`
   - `risc_os_filetype` -- lowercase hex string if detected by RISC OS filetype (e.g. `'a91'`), otherwise `None`
   - `extensions` -- list of file extensions for fallback detection (e.g. `['.zip']`), or `[]`
   - `tool` -- name of the extraction tool (informational)
   - `extract_creates_dir` -- `True` for multi-file archives, `False` for single-file compressors
3. Add an extraction branch for the new type inside `process_archive_extract` in
   `worker/arcworker/analysis.py`, calling the appropriate tool wrapper.
4. **`is_acorn_archive`** -- if the format stores RISC OS `,xxx` filetype suffixes
   on filenames (e.g. `ReadMe,fff`), add the new `ArchiveType` to the
   `is_acorn_archive` set near the top of the file-registration loop in
   `process_archive_extract`. This enables suffix parsing and strips the `,xxx`
   from display paths. Formats that need this: any RISC OS native archive
   (`ARCFS`, `SPARK`, `ZIP_RISCOS`, `PACKDIR`, `TBAFS`, `CFS`, `SQUASH`, `FCFS`).
   Plain PC archives do **not** need it.
5. Update the format table in `doc/ARCHIVE_EXTRACTION.md`.

### Database Changes

Database schema changes are managed with [Flask-Migrate](https://flask-migrate.readthedocs.io/), which wraps [Alembic](https://alembic.sqlalchemy.org/). All models live in `myapp/database.py`. When you change a model, you create a migration script that records the change, then apply it to the database.

#### First-Time Setup

If the `migrations/` directory doesn't exist yet (fresh clone), initialise it:

```bash
flask db init
flask db migrate -m "Initial migration"
flask db upgrade
```

This creates the `migrations/` directory structure and generates an initial migration from the current models.

> **Note:** The Docker entrypoint (`Dentrypoint.sh`) runs `flask db upgrade` and `flask create-admin` on every start (both are idempotent).

#### Typical Workflow

1. **Edit the models** in `myapp/database.py` (add columns, tables, change types, etc.).

2. **Generate a migration** -- Alembic diffs the models against the database and creates a script:

   ```bash
   flask db migrate -m "Add description column to platforms"
   ```

3. **Review the migration** before applying. Auto-generated migrations are not perfect -- check the file in `migrations/versions/`:

   ```bash
   # The newest file is your migration
   ls -t migrations/versions/*.py | head -1
   ```

   Things to watch for:
   - Table or column renames are detected as a drop + create (data loss). Manually edit these to use `op.alter_column()` or `op.rename_table()`.
   - Changes to enum values may need manual handling.
   - Check that both `upgrade()` and `downgrade()` functions look correct.

4. **Apply the migration:**

   ```bash
   flask db upgrade
   ```

#### Quick Reference

| Command | What it Does |
|---------|-------------|
| `flask db init` | Create the `migrations/` directory (one-time only) |
| `flask db migrate -m "message"` | Auto-generate a migration from model changes |
| `flask db upgrade` | Apply all pending migrations to the database |
| `flask db downgrade` | Undo the last migration |
| `flask db current` | Show which migration the database is currently at |
| `flask db history` | List all migrations in order |
| `flask db heads` | Show the latest migration(s) |
| `flask db show <revision>` | Show details of a specific migration |
| `flask db stamp head` | Mark the database as up-to-date without running migrations (useful when the schema already matches the models, e.g. after `db.create_all()`) |

#### Tips

- **Always review generated migrations.** Alembic's auto-detection is good but not infallible. It cannot detect renamed columns/tables, changes within existing enum types, or changes to constraints that aren't reflected in the model metadata.
- **Commit migrations to version control.** They are part of the project history and other developers will need them.
- **One logical change per migration.** Don't batch unrelated schema changes into a single migration -- it makes rollbacks harder.
- **If you get "Target database is not up to date"**, run `flask db upgrade` first to bring your database to the latest migration before generating a new one.
- **If you get "Can't locate revision"** after pulling changes, you may need to `flask db upgrade` to apply migrations created by others.
- **To start fresh** during development (throwing away all data), drop the database and re-run `flask db upgrade`.

### Code Style

- **Indentation**: 4 spaces per level (PEP 8). Do not use tabs.
- **Vim modeline**: every Python file ends with a blank line followed by `# vim: ts=4 sw=4 et`. This ensures editors with modeline support respect the project style automatically.
- **Editor setup**: configure your editor to use spaces, not tabs, with a tab width of 4:
  - Vim/Neovim: `set ts=4 sw=4 et` (or rely on the modeline)
  - VS Code: `"editor.insertSpaces": true, "editor.tabSize": 4`
  - PyCharm: Preferences → Code Style → Python → Use tab character (off), Tab size: 4
- Python 3.10+ type hints are used in newer code.
- Keep blueprints focused -- each module should handle one feature area.
- Use UUIDs for public-facing identifiers (URLs, API responses) rather than sequential IDs.

### Submitting Changes

1. Fork the repository and create a feature branch.
2. Make your changes, keeping commits focused and well-described.
3. Test your changes locally (both web UI and worker if applicable).
4. Open a pull request with a description of what changed and why.

## License

Arcology is licensed under the MIT License. By contributing, you agree that your contributions will be licensed under the same terms.
