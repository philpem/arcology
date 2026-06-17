# Arcology

A digital artefact catalogue for retrocomputing collections, built on Flask.

## Features

- **Catalogue items** with multiple digital artefacts (disk images, scans, etc.)
- **Upload files directly** with automatic type detection
- **Automatic analysis pipeline** - flux images are decoded, file listings extracted
- **Link to external systems** (Koillection, Collective Access, etc.)
- **Browse extracted file listings** from disk images
- **Identify known files** using hash databases
- **REST API** for integration with tools
- **User authentication** with Flask-Login

## Quick Start with Docker

```bash
# Clone/extract the project
cd arcology

# Create data directories
mkdir -p data/uploads data/outputs data/db data/chunks

# Build and start (first build takes a while - compiles analysis tools)
docker compose up --build -d

# Watch logs
docker compose logs -f

# Access at http://localhost:8000
```

### Docker Commands

```bash
# Build containers (required after code changes)
docker compose build

# Start services
docker compose up -d

# Start with multiple analysis workers
docker compose up -d --scale worker=4

# View logs
docker compose logs -f web      # Web app logs
docker compose logs -f worker   # Worker logs

# Restart after changes
docker compose up --build --force-recreate -d

# Stop everything
docker compose down

# Stop and remove volumes (WARNING: deletes data)
docker compose down -v
```

See [doc/ADMIN_COMMANDS.md](doc/ADMIN_COMMANDS.md) for admin CLI commands
(rebuild-search-index, rescan-hashes, reanalyse, etc.).

For larger deployments — splitting workers into specialised pools (e.g.
flux-decode vs lightweight metadata), running on Kubernetes, or giving
web-UI uploads queue priority over bulk `arco` imports — see
[doc/WORKER_POOLS.md](doc/WORKER_POOLS.md).

### Database browser (Adminer)

Adminer is not started by default (it provides unauthenticated direct database
access and must never run in production). Use the separate override file when
you need it for debugging:

```bash
# Start adminer alongside the main stack (localhost only — port 8080)
docker compose -f docker-compose.yml -f docker-compose.adminer.yml up -d

# Or attach adminer to an already-running stack
docker compose -f docker-compose.yml -f docker-compose.adminer.yml up -d adminer

# One-liner — no compose file required
docker run --rm -p 127.0.0.1:8080:8080 --network arcology_default adminer
```

Access at http://localhost:8080 — connect with server `db`, username
`arcology_user`, database `arcology`.

### Production Configuration

Set a persistent `SECRET_KEY` to avoid losing user sessions on restart:

```bash
python3 -c 'import secrets; print(f"SECRET_KEY={secrets.token_urlsafe(32)}")' >> .env
```

If `SECRET_KEY` is not set (or left at the default placeholder), Arcology generates a random key at startup and logs a warning. Sessions will not survive a restart in that case.

### Worker API Key

Workers authenticate to the web API using a pre-shared key. You must generate one and set it on **both** the web and worker containers before starting the stack:

```bash
python3 -c 'import secrets; print(f"WORKER_API_KEY=wrk_{secrets.token_urlsafe(32)}")' >> .env
```

Both services read `WORKER_API_KEY` from the `.env` file automatically via Docker Compose. The worker will refuse to start if this variable is not set. Admins can view the configured key in the Admin panel (useful for adding additional workers later).

### Configuration Options

Arcology can be configured via environment variables. Create a `.env` file or set environment variables in docker-compose.yml:

#### Archive Extraction

```bash
# Maximum depth for recursive archive extraction (default: 10)
# Prevents infinite loops from self-referential archives (quines, trojans, matryoshka archives)
MAX_ARCHIVE_DEPTH=10
```

When an archive contains nested archives (e.g., ZIP within ZIP within ZIP), extraction will stop at the configured depth. Files at the maximum depth are marked but not extracted.

#### Other Settings

```bash
# Flask configuration
SECRET_KEY=<your-secret-key>      # Generate with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# Worker authentication (required - set on both web and worker containers)
WORKER_API_KEY=<your-worker-key>  # Generate with: python3 -c 'import secrets; print(f"wrk_{secrets.token_urlsafe(32)}")'

# Worker configuration (usually auto-configured in Docker)
ARCOLOGY_API=http://web:8000/api  # API endpoint URL
UPLOAD_DIR=/data/uploads          # Uploaded files directory
OUTPUT_DIR=/data/outputs          # Analysis outputs directory
POLL_INTERVAL=10                  # Ceiling of the idle poll backoff (seconds)
LOG_LEVEL=INFO                    # Logging level: DEBUG, INFO, WARNING, ERROR
TOOL_TIMEOUT=3600                 # Subprocess timeout for external tool execution (seconds)
MAX_DECOMPRESSED_BYTES=10737418240  # Decompression size cap in bytes (default: 10 GiB)
MASTERING_TRACK_SCAN_COUNT=5      # Number of trailing tracks scanned for mastering fingerprints
```

## Quick Start (Development)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp myapp/myapp.cfg.example myapp/myapp.cfg
# Edit myapp/myapp.cfg - set SECRET_KEY

# Apply database migrations
flask db upgrade

# Create admin user (interactive prompt)
flask create-admin

# Run development server
python -m myapp
```

Visit http://localhost:5000

## CLI Client (`arco`)

The `arco` command-line tool lets you manage items, upload artefacts, bulk-import
file archives, and manage hash databases from your terminal.

### Installation

```bash
# On a client machine, install a released wheel (no repo checkout needed) —
# see the Releases page for the latest cli-v* tag:
pip install https://github.com/philpem/arcology/releases/download/cli-v0.2.0/arcology_cli-0.2.0-py3-none-any.whl

# Or straight from git (pip clones and builds the package):
pip install "arcology-cli @ git+https://github.com/philpem/arcology.git#subdirectory=cli"

# Or with pipx (isolated install, no virtualenv needed)
pipx install git+https://github.com/philpem/arcology.git#subdirectory=cli

# From a development checkout: run directly, or editable install
python cli/arco --help
pip install -e cli/
```

### Setup

```bash
arco configure            # Interactive setup (server URL + API key)
arco health               # Verify connectivity
```

### Common commands

```bash
arco items list            # List items
arco items create -n "…"   # Create item
arco upload ITEM_UUID f.scp # Upload artefact
arco download ART_UUID     # Download artefact
arco platforms             # List platforms

# Bulk import a directory tree (see doc/BULK_IMPORT.md for the full guide:
# disk-image dedup, sidecar bundling for drive images, size limits, etc.)
arco bulk-import --archive-dir ~/discs --tag myimport --platform "BBC Micro"

# Hash database management
arco hashdb list
arco hashdb export 1 riscos_apps.json
arco hashdb import riscos_apps.json

# Build a RISC OS application hash database from imported disc images.
# Selects items by tag (or --item/--platform), parses each app's !Run to mark
# the launched executable Mandatory, and emits import-ready JSON.
arco hashdb generate-riscos --tag arcarc --db-name "Arcarc RISC OS" --output riscos-hashdb.json
arco hashdb import riscos-hashdb.json

# Add --explain to find out why some applications produced no mandatory file
# (no launch target found, target already in a hash database, shared, etc.).
arco hashdb generate-riscos --item <uuid> --db-name "Apps" --output apps.json --explain

# Regenerating a database whose own files are already known? --include-known
# stops those files being excluded for being in an active hash database.
arco hashdb generate-riscos --item <uuid> --db-name "Apps" --output apps.json --include-known
```

Run `arco --help` or `arco <command> --help` for full usage details.

## Project Structure

```
arcology/
├── myapp/
│   ├── app.py              # Application factory
│   ├── database.py         # SQLAlchemy models
│   ├── extensions.py       # Flask extensions
│   ├── myapp.cfg           # Configuration (optional; env vars take precedence)
│   ├── blueprints/         # Feature modules
│   │   ├── dashboard.py    # Homepage
│   │   ├── items.py        # Item CRUD
│   │   ├── artefacts.py    # Artefact management + upload
│   │   ├── taxonomy.py     # Platforms, categories, tags
│   │   ├── analysis.py     # Analysis queue
│   │   └── api.py          # REST API
│   ├── templates/          # Jinja2 templates
│   └── static/             # CSS, JS, images
├── worker/
│   ├── Dockerfile          # Worker container with analysis tools
│   └── worker.py           # Analysis worker script
├── docker-compose.yml      # Docker orchestration
├── Dockerfile              # Web app container
├── .env.example            # Environment template
├── requirements.txt
└── README.md
```

## Analysis Pipeline

When you upload a flux image (SCP, KF, etc.), the system automatically:

1. **Flux Visualisation** - Generates flux plots using Fluxfox and HxCFE
2. **Flux Decode** - Converts to sector formats (IMD, HFE, IMG)
3. **File Listing** - Extracts directory listings from decoded images
4. **Hash Matching** - Identifies known files using hash databases

Each derived artefact (e.g., decoded IMG from SCP) triggers its own analysis chain.

### Analysis Tools (in worker container)

- **Fluxfox** (imgviz) - Flux visualisation
- **HxCFE** - Flux conversion and visualisation
- **Greaseweazle** (gw) - Sector image conversion
- **DiscImageManager** - Acorn filesystem extraction
- **7z** - DOS/FAT/ISO extraction

## API Endpoints

- `GET /api/health` - Health check (unauthenticated; returns `{"status":"healthy"}`)
- `GET/POST /api/items` - List/create items
- `GET/PUT/DELETE /api/items/{id}` - Item operations
- `POST /api/items/{id}/artefacts` - Add artefact
- `POST /api/items/{id}/artefacts/upload` - Upload artefact file (multipart)
- `GET/DELETE /api/artefacts/{id}` - Get/delete artefact
- `GET /api/artefacts/{id}/download` - Download file
- `POST /api/artefacts/{id}/analysis` - Queue analysis
- `GET /api/outputs/{filename}` - Get analysis output (visualisation, etc.)
- `GET /api/analysis/pending` - Get pending jobs (for worker)
- `PUT /api/analysis/{id}` - Claim job or post result (for worker)
- `GET /api/platforms` - List platforms
- `GET /api/categories` - List categories
- `GET /api/tags` - List tags
- `GET /api/lookup?system=…&ref=…` - Find item by external system reference
- `GET /api/hash-lookup?md5=…` or `?sha1=…` - Find known files by hash
