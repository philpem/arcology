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
- `myapp/database.py` -- All SQLAlchemy models and the web-specific enums (`AnalysisStatus`, `FilesystemType`, etc.). `ArtefactType` and `AnalysisType` are imported from `arcology_shared/enums.py`.
- `myapp/myapp.cfg` -- Runtime configuration (database URI, secret key, upload paths, etc.). Copied from `myapp.cfg.example`.

**Blueprints** (`myapp/blueprints/`) -- each feature area is a separate Flask blueprint:

| Blueprint | Purpose |
|-----------|---------|
| `dashboard.py` | Homepage with collection statistics |
| `items.py` | CRUD for catalogue items (search, filter, pagination) |
| `artefacts.py` | File upload, type detection, artefact management. Also contains the `ANALYSIS_MAP` that determines which analyses are auto-queued for each artefact type. |
| `taxonomy.py` | Platforms, categories, tags, external systems, hash databases |
| `analysis.py` | Analysis queue UI (view, cancel, retry jobs) |
| `search.py` | Global cross-item search using a prefix query syntax. See table below for all supported keys. |
| `api.py` | REST API endpoints consumed by workers and external tools |

Blueprints are auto-discovered and registered -- any module in `myapp/blueprints/` that defines a `blueprint` variable will be loaded automatically. Modules can also provide an `init_app(app)` function for additional setup (e.g., the API blueprint uses this to exempt itself from CSRF).

**Search query syntax** — `search.py` parses a structured prefix syntax. Multiple values for the same key are OR'd; different keys are AND'd. Bare words search item/artefact names and descriptions.

| Key | Aliases | Matches |
|-----|---------|---------|
| `filename:` | `file:` | Extracted file path (substring) |
| `path:` | | Extracted file path (substring, same as `filename:`) |
| `ext:` | | File extension (e.g. `ext:bas`) |
| `type:` | `filetype:` | RISC OS filetype — 3-digit hex (`type:fea`) or name (`type:Desktop`) |
| `label:` | `disc:` | Partition/disc label |
| `ident:` | `gnu:`, `gnufile:` | GNU `file` type string from `PARTITION_DETECT` |
| `fs:` | `filesystem:` | Filesystem type (e.g. `fs:adfs`) |
| `protection:` | `prot:` | Copy-protection indicator type (e.g. `protection:bad_crc`) |
| `mastering:` | | Mastering indicator type (e.g. `mastering:formaster`) |
| `tag:` | | Artefact tag name |
| `md5:` | | MD5 hash of extracted file or artefact |
| `sha1:` | | SHA-1 hash of extracted file |
| `sha256:` | | SHA-256 hash of extracted file or artefact |
| `module:` | | RISC OS module title (e.g. `module:WindowManager`) |
| `command:` | | RISC OS star command provided by a module (e.g. `command:Desktop`) |
| `swi:` | | RISC OS SWI name provided by a module (e.g. `swi:Wimp_Poll`) |

Values support `*` as a wildcard (e.g. `filename:*.bas`). Results are capped at 200 items per bucket.

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
- `arcology_shared/enums.py` -- Canonical `ArtefactType` and `AnalysisType` enum definitions, imported by both web app and worker.
- `arcology_shared/archive_formats.py` -- Canonical archive format definitions (`ArchiveType`, `ARCHIVE_FORMATS`, helpers), imported by the worker.

**How workers process jobs:**

1. The worker polls `GET /api/analysis/pending` on a configurable interval (default 10s).
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
| `FILE_EXTRACTION` | 7z, DiscImageManager | Extracts files, registers the listing, and detects archives inline (queues `ARCHIVE_EXTRACT`) |
| `ARCHIVE_EXTRACT` | 7z, ArcFS tools | Extracts nested archives and registers contained files |
| `METADATA_EXTRACT` | (built-in) | Computes hashes and extracts format metadata |
| `PARTITION_DETECT` | sfdisk, ADFS signature detection, `file` | Detects partitions and filesystem types |
| `DISC_PROTECTION_DETECT` | HxCFE / hfe_parser | Scans for copy protection indicators (bad CRC, weak bits, DDAM, ID mismatches) |
| `DISC_MASTERING_DETECT` | HxCFE / hfe_parser | Scans trailing tracks for mastering/duplicator fingerprints (traceback, formaster) |
| `FORMAT_IDENTIFY` | (built-in) | Identifies file format by magic bytes (FCFS images, misidentified archives); displayed as "File Format Identify" |
| `CHECKSUM_COMPUTE` | (built-in) | Computes MD5/SHA-1/SHA-256 hashes for an artefact |
| `PRODUCT_RECOGNITION` | (built-in) | Matches extracted file hashes against known-file databases |
| `SECTOR_DUMP` | (built-in) | Extracts a raw sector dump from a flux image |
| `ARMLOCK_REMOVE` | (built-in) | Removes ARMlock disc security from RISC OS disc images |
| `FORMAT_CONVERT` | drawfile-render, spritefile | Converts Acorn/RISC OS native formats (Draw, Sprite, text) to portable equivalents (SVG, PNG) for inline viewing |
| `RISCOS_MODULE_PARSE` | (built-in, Peter Howkins' module parser) | Extracts metadata (title, version, SWIs, star commands) from RISC OS relocatable module files (filetype FFA) |

**Worker environment variables** (all read at startup from the environment):

| Variable | Default | Description |
|----------|---------|-------------|
| `ARCOLOGY_API` | `http://host.docker.internal:5000/api` | Web API base URL |
| `UPLOAD_DIR` | `/data/uploads` | Path to uploaded artefact files |
| `OUTPUT_DIR` | `/data/outputs` | Path for analysis output files |
| `WORKER_API_KEY` | (required) | Pre-shared key for API authentication |
| `STORAGE_BACKEND` | `local` | Storage backend: `local` (default) or `s3` (S3-compatible) |
| `S3_ENDPOINT_URL` | (required for S3) | S3 endpoint URL (e.g. `https://s3.amazonaws.com`) |
| `S3_BUCKET` | (required for S3) | S3 bucket name |
| `S3_ACCESS_KEY` | (required for S3) | S3 access key ID |
| `S3_SECRET_KEY` | (required for S3) | S3 secret access key |
| `S3_REGION` | (optional for S3) | S3 region (e.g. `us-east-1`) |
| `POLL_INTERVAL` | `10` | Ceiling of the idle poll backoff (seconds) |
| `POLL_BACKOFF_FLOOR` | `0.5` | Floor of the idle poll backoff (seconds) |
| `TOOL_TIMEOUT` | `3600` | Per-job subprocess timeout in seconds |
| `MAX_ARCHIVE_DEPTH` | `10` | Maximum nested archive extraction depth |
| `MAX_DECOMPRESSED_BYTES` | `10737418240` (10 GiB) | Maximum decompressed size; guards against decompression bombs |
| `MASTERING_TRACK_SCAN_COUNT` | `5` | Number of trailing tracks scanned for mastering fingerprints |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

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

The worker uses a `arcology_shared/` package that lives in the repo root. When running
the worker locally (outside Docker) you must ensure the repo root is on the
Python path. The entry point (`worker/worker.py`) handles this automatically,
but you must run it from the **repo root** or use `PYTHONPATH`:

```bash
# From the repo root (recommended):
python worker/worker.py

# Or with an explicit PYTHONPATH if running from another directory:
PYTHONPATH=/path/to/arcology python worker/worker.py
```

Inside Docker the `arcology_shared/` directory is copied into the container at build
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
├── arcology_shared/            # Shared definitions (used by web app and worker)
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

1. Add the new type to `AnalysisType` in `arcology_shared/enums.py`.
2. Add it to the `ANALYSIS_MAP` in `myapp/blueprints/artefacts.py` so it gets auto-queued for the appropriate artefact types.
3. Implement a `process_<type>` handler method in `worker/arcworker/analysis.py`.
4. Register the handler in the `handlers` dict inside `AnalysisWorker.process_analysis()`.
5. Write a migration to add the value to the PostgreSQL `analysistype` enum (see [Enum case pitfall](#enum-case-pitfall) below).

#### Enum case pitfall

**This has tripped us up more than once.** SQLAlchemy stores Python `enum.Enum`
members using their `.name` (the Python identifier), **not** their `.value`. So
`AnalysisType.FILE_EXTRACTION` (whose `.value` is `"file_extraction"`) is stored
in PostgreSQL as the string `'FILE_EXTRACTION'` (uppercase).

The `analysistype` PostgreSQL enum therefore contains uppercase strings like
`'FILE_EXTRACTION'`, `'ARCHIVE_EXTRACT'`, `'PRODUCT_RECOGNITION'`. **Always use the
uppercase name in `ADD VALUE` migrations:**

```python
# CORRECT
op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'MY_NEW_TYPE'"))

# WRONG — will fail at runtime with:
# invalid input value for enum analysistype: "MY_NEW_TYPE"
op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'my_new_type'"))
```

This applies to every `SQLEnum(SomePythonEnum)` column in the project: `analysistype`,
`artefacttype`, `analysisstatus`, `filesystemtype`, etc.

#### Rollback pitfall: stale enum-backed rows

Adding a PostgreSQL enum value is effectively one-way here. Reverting the code
can remove an `AnalysisType` member from `arcology_shared/enums.py`, but any existing
rows in `analyses` that still use that enum name will remain in the database.
Once that happens, ORM queries can fail at row-materialisation time with errors
like:

```text
LookupError: 'DETECT_TRACK_DENSITY' is not among the defined enum values
```

Before reverting code that removes an analysis enum, first clean up or rewrite
any rows that reference it. For a fully removed analysis type, that usually
means deleting stale rows from `analyses` before switching the code back.

**Protection and mastering indicator types** (`ArtefactProtection.protection_type` and `ArtefactMastering.mastering_type`) are free-text strings stored by the worker — they are not enums. Known values are documented in comments in `myapp/database.py`. If you introduce new indicator types in a worker tool, use short lowercase snake_case names (e.g. `bad_crc`, `formaster`); the search UI will surface them automatically once they appear in the database.

### Adding a New Artefact Type

1. Add the type to `ArtefactType` in `arcology_shared/enums.py`.
2. Update the file extension detection logic in `myapp/blueprints/artefacts.py`.
3. Add entries to `ANALYSIS_MAP` to specify which analyses should auto-run.

### Adding a New Archive Format

Archive format definitions live in `arcology_shared/archive_formats.py`.

1. Add the new type to the `ArchiveType` enum in `arcology_shared/archive_formats.py`.
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
   - Changes to enum values may need manual handling. See [Enum case pitfall](#enum-case-pitfall): use the **uppercase** enum name, not the lowercase value.
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
- **Never use placeholder revision IDs** like `a1b2c3d4e5f6`. They look unique but collide when two authors independently pick the same sequential pattern, causing Alembic to report duplicate revisions and a cycle error. For hand-written migrations, generate an ID from the current UTC timestamp: `python3 -c "import time; print(hex(int(time.time()))[2:].zfill(12))"`. For Alembic-generated migrations (`flask db migrate`), keep the generated `revision` unchanged.
- **Migration filenames are ordering keys.** Name files `YYYYMMDD_HHMMSS_description.py` in UTC. The timestamp prefix must sort in the same order as the Alembic `down_revision` chain so the last listed file is the current head. For new migrations, use the current UTC time. If a migration is rebased or reordered to resolve merge conflicts, rename the file so lexicographic filename order still matches migration order. Do not use filesystem modification times or the header `Create Date` for this. Keep the filename timestamp aligned with the hex `revision` timestamp where practical. This repository's Alembic template is already configured for second-precision filenames in `migrations/alembic.ini`.
- **If you get "Target database is not up to date"**, run `flask db upgrade` first to bring your database to the latest migration before generating a new one.
- **If you get "Can't locate revision"** after pulling changes, you may need to `flask db upgrade` to apply migrations created by others.
- **To start fresh** during development (throwing away all data), drop the database and re-run `flask db upgrade`.
- **Watch out for migration branch conflicts.** If two PRs both add a migration at the same time, they'll share the same `down_revision` and cause a "Multiple head revisions" error after merge. CI detects this automatically on PRs. You can also install the local pre-push hook: `git config core.hooksPath hooks`. See [doc/MIGRATION_CONFLICTS.md](doc/MIGRATION_CONFLICTS.md) for details.

### Worker and Analysis Pipeline: Common Pitfalls

#### `FORMAT_CONVERT` view icons not appearing

The file listing shows an eye icon next to a file when its path appears in `viewable_filenames`. That set is built by collecting every `source_file` value from completed `FORMAT_CONVERT` analysis outputs. **The value must be a byte-for-byte match against `ExtractedFile.path` as stored in the database.**

Common causes of a mismatch:

- **Case differences.** `ExtractedFile.path` goes through `sanitize_path()` which passes the string as-is (no case folding). If the worker stores `source_file = 'myfile.txt'` but the DB has `'MyFile,fff'` (with `,xxx` suffix stripped in one place but not the other), the lookup silently fails.
- **Backslash vs forward slash.** Always use forward slashes; `sanitize_path()` normalises separators on write, but hand-assembled paths in the worker must match.
- **Leading slash or extra path component.** `ExtractedFile.path` is relative to the extraction root (no leading slash). Make sure `source_file` is built the same way — typically `str(path.relative_to(extract_dir))`.
- **Acorn `,xxx` suffix.** When `acorn=True` or `acorn='auto'` is passed to `enumerate_extracted_files()`, the `,xxx` suffix is stripped from the stored path. The worker's `FORMAT_CONVERT` handler must strip the same suffix when constructing `source_file`, or the comparison will never match.

Quick diagnostic: add a temporary `log.info()` to print `source_file` values alongside `ExtractedFile.path` values and diff them.

#### `viewable_filenames` scope: always use `all_artefact_ids`

The artefact detail page assembles `all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)` and uses it for all partition/file queries. The `viewable_filenames` query **must** also use this list, not just `artefact.id`.

If you scope it to a single artefact ID, `FORMAT_CONVERT` analyses that ran against a *derived* artefact (e.g. an ISO that was extracted from a ZIP) will be invisible, even though their output files are shown in the listing. The symptom is that `FORMAT_CONVERT` appears completed and the SVG files exist in `data/outputs/`, but no eye icons appear.

#### Physical path vs DB display path

The worker extracts files to a temporary directory. The path stored in `ExtractedFile.path` is the **display path** — the filename as it would appear to a user. For RISC OS disc images this may differ from the on-disk name:

- **Acorn `,xxx` suffix**: on disk the file may be named `!Run,feb`; the DB stores `!Run`.
- **RISC OS ISO pling-renaming**: ISO 9660 forbids `!` so tools store `_PAINT` on disc. `FILE_EXTRACTION` physically renames these directories to `!PAINT` during extraction, so subsequent handlers (`ARCHIVE_EXTRACT`, `RISCOS_MODULE_PARSE`, `FORMAT_CONVERT`) can open the file at the pling-correct path without any reverse-lookup.
- **INF sidecar renaming**: some tools write files with DOS-encoded characters (e.g. `?` instead of BBC `#`). `process_inf_sidecars()` renames these to the original BBC names before enumeration, so on-disk names match DB paths.

If you add a new handler that opens extracted files by DB path, the physical file **will** be at that path (because of the renames above) — no special translation is needed. If you bypass the physical rename and try to open files by their raw names, you will get file-not-found errors.

#### `acorn` mode and `filetype_map` in `enumerate_extracted_files()`

`enumerate_extracted_files()` supports two separate RISC OS metadata mechanisms that should be used together for ISOs:

- `acorn='auto'` — scans filenames for `,xxx` hex suffixes (e.g. from Rock Ridge NM entries) and strips them from display paths, storing the hex value as `risc_os_filetype`.
- `filetype_map` — a `{lowercase_path: filetype_hex}` dict derived from the ARCHIMEDES ISO 9660 extension. Applied *after* suffix-based detection; entries already having a `risc_os_filetype` from the suffix are not overwritten.

For RISC OS ISOs, always pass both. RISC OS CD mastering tools sometimes use Rock Ridge names with `,xxx` suffixes, sometimes ARCHIMEDES blocks, and sometimes both. Passing only one will leave filetypes `NULL` for files that used the other mechanism.

#### `extraction_started_at` and invalid timestamps

Many platforms and filesystems do not store a usable file timestamp — BBC DFS has none at all, ADFS only date-stamps some files, and any no-RTC machine writes bogus dates. When the source carries no date, extraction tools (DIM, 7z, …) default the extracted file's mtime to *now*, which would otherwise be catalogued as today's date.

Pass `extraction_started_at` (the UTC instant the extraction job began) to `enumerate_extracted_files()`. Any file whose timestamp falls within the extraction window `[started_at - 60s, now]` is dropped to `NULL` (unknown) rather than stored — that date can only be the tool's fabricated "now". Independently, timestamps in the future or before `1975-01-01` (`_TIMESTAMP_FLOOR`) are always dropped, which also catches corrupt RISC OS load/exec decodes. Genuinely old dates — including INF-decoded RISC OS date-stamps — are preserved. The goal is to keep every valid timestamp while discarding patently invalid ones.

#### Derived artefacts and follow-on analysis chains

Registering a derived artefact via the API causes the web app to check `ANALYSIS_MAP` and automatically queue follow-on analyses. This is intentional, but it means a chain of several workers can be processing the same original artefact concurrently. Keep handlers idempotent and do not assume that derived artefacts registered in one job are visible to handlers running in parallel.

#### Worker has no direct database access

All data exchange goes through the REST API. If you need information from the database in a worker handler (e.g. previously registered file paths), you must expose it via an API endpoint and fetch it from the worker. Do not add SQLAlchemy imports to the worker package.

#### `_arcology_iso_meta.json` sidecar file

`FILE_EXTRACTION` writes a JSON sidecar `_arcology_iso_meta.json` into the extraction output directory. It carries metadata (currently the ARCHIMEDES `filetype_map`) that `FORMAT_CONVERT` cannot cheaply recompute. The sidecar lives alongside the extracted files in `data/outputs/`. If you add new persistent metadata to this sidecar, document its schema in a comment near the write site in `analysis.py`.

#### Filename normalisation (`normalize_extracted_filenames`)

External extraction tools (DIM, 7z) may write filenames containing raw byte
sequences — for example, RISC OS Latin-1 characters in the 0x80–0x9F range that
the kernel stores as surrogate-escaped strings on Linux.
`normalize_extracted_filenames(root)` in `worker/arcworker/utils/text.py` walks
the extraction directory bottom-up and renames these files to their correct
Unicode equivalents.

It must be called **before** `enumerate_extracted_files()` and before INF
sidecar processing.  Currently called by `extract_acorn_disc_image_manager()`
(RISC OS Latin-1 decoder) and `extract_dos_7z()` (CP850 decoder via the
`decoder` parameter).  See `doc/format_info/acorn32bit/risc_os_character_set.md`
for details on the RISC OS character mapping.

When adding a new extraction tool that may produce non-ASCII filenames, call
`normalize_extracted_filenames(output_dir)` with an appropriate `decoder`
function before returning.

#### RISC OS INF sidecar files

Some extraction tools (currently Disc Image Manager; others may follow) produce
`.inf` sidecar files alongside extracted data files.  These carry RISC OS
metadata that cannot be represented on Unix filesystems:

```
<filename> <load_hex> <exec_hex> [<length_hex>] [<access>] [<extra info>]
```

`process_inf_sidecars()` in `worker/arcworker/tools/extraction.py` is the
reusable pre-processing step.  Each extraction tool that produces INF files is
responsible for calling it **before** returning, so that the INFs are processed
and deleted before `enumerate_extracted_files()` runs.  Currently only
`extract_acorn_disc_image_manager()` calls it.  The collected metadata dict is
returned in the tool's result under the key `'inf_metadata'`, which the caller
passes through to `enumerate_extracted_files()`.

The function:

1. Finds `.inf` files (case-insensitive: `.inf`, `.INF`, `.Inf`, etc.).
2. Validates each has a matching data file (same path minus the `.inf` extension).
3. Parses the INF to extract load address, exec address, filetype, and attributes.
4. Renames the data file from its DOS-encoded name to the original BBC name using
   this character translation table:

   | BBC | DOS |
   |-----|-----|
   | `#` | `?` |
   | `.` | `/` |
   | `$` | `<` |
   | `^` | `>` |
   | `&` | `+` |
   | `@` | `=` |
   | `%` | `;` |

   The rename only fires when the on-disk name matches the DOS-encoded version of
   the BBC name from the INF.  If the file is already named correctly (e.g. DIM
   output), no rename occurs.

5. Deletes the INF file.
6. Returns a metadata dict that `enumerate_extracted_files()` merges into file
   records via its `inf_metadata` parameter.

The following `ExtractedFile` columns are populated from INF metadata:

| Column | Type | Example | Source |
|--------|------|---------|--------|
| `load_address` | String(8) | `'fffffd00'` | INF field 2, zero-padded lowercase hex |
| `exec_address` | String(8) | `'ffffff00'` | INF field 3, zero-padded lowercase hex |
| `risc_os_filetype` | String(3) | `'ffd'` | Derived from load address bits 19:8 when date-stamped |
| `attributes` | String(50) | `'WR/r'` | INF field 5, stored as-is (letters or hex) |

**Extending to other tools:**  If a new extraction tool produces standard `.inf`
sidecar files, call `process_inf_sidecars(output_dir)` inside the tool's
extraction wrapper (before it returns) and include the result in the return dict
as `'inf_metadata'`.  See `extract_acorn_disc_image_manager()` for the pattern.
The caller must then pass this dict through to `enumerate_extracted_files()` via
its `inf_metadata` parameter.  If the tool uses a different INF format or
filename translation scheme, extend `_parse_inf_line()` or add a
`translation_table` parameter to `process_inf_sidecars()`.  The translation
table is defined as `_BBC_TO_DOS` / `_DOS_TO_BBC` in `extraction.py`.

Tests: `ci/test_inf_processing.py` (44 tests covering parsing, translation, and
end-to-end `process_inf_sidecars()` behaviour).

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
