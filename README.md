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

The analysis worker requires a Linux host with **Landlock ABI 3 or newer**
(upstream Linux **6.2+**). See the [minimum versions](#minimum-versions) below
before starting workers; containers use the host kernel.

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

### Minimum versions

| Component | Minimum | Applies to |
|-----------|---------|------------|
| Python | 3.10 | Web application and worker |
| Linux kernel | 6.2 with Landlock ABI ≥3 enabled | Worker host, including Docker/Podman hosts and the Linux VM used by Docker Desktop |
| Architecture | amd64 (x86_64) or arm64 (aarch64) | Worker's Landlock launcher |

Landlock first appeared in Linux 5.13, but older ABIs cannot restrict file
truncation. Arcology requires ABI 3 and fails closed: it refuses to start a
worker, or launch a tool, when confinement cannot be enforced. No unsandboxed
fallback is available. The web application does not need Landlock and does
not launch external programs; its version comes from the build-provided
`VERSION` file. See the [kernel's Landlock ABI documentation](https://www.kernel.org/doc/html/latest/userspace-api/landlock.html).

These are the first distribution releases whose standard kernels meet the
worker's ABI requirement. This is a general kernel baseline, not a list of
currently maintained releases or a guarantee for every kernel flavour. Use a
maintained release and verify support in the actual worker environment.

| Host distribution | Minimum release with a suitable standard kernel | Kernel at release | Notes |
|-------------------|------------------------------------------------|-------------------|-------|
| Ubuntu | [23.04](https://discourse.ubuntu.com/t/lunar-lobster-release-notes/31910) | 6.2 | First qualifying interim release; for an LTS with a qualifying GA kernel use [24.04](https://documentation.ubuntu.com/release-notes/24.04/) (6.8). [22.04.3 with the HWE kernel](https://certification.canonical.com/docs/programmes/pdf/server/Policy_Guide.pdf/) also qualifies; 22.04's GA 5.15 kernel does not. |
| Debian | [13 (trixie)](https://www.debian.org/releases/trixie/release-notes/whats-new.en.html) | 6.12 | Debian 12's standard 6.1 kernel is too old; an appropriately enabled newer/backports kernel can qualify. |
| Alpine Linux | [3.19](https://www.alpinelinux.org/posts/Alpine-3.19.0-released.html) | 6.6 | Alpine 3.18's standard 6.1 kernel is too old. |
| Rocky Linux | [10.0](https://docs.rockylinux.org/releases/release_notes/10_0/) | 6.12 | Landlock was introduced in the [RHEL 10 kernel baseline](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html-single/10.0_release_notes/index#enhancement_kernel); do not infer support from Rocky 8/9's kernel version or assume a backport. |

The running kernel must have `CONFIG_SECURITY_LANDLOCK=y` and include
`landlock` in its active LSM list (normally visible in
`/sys/kernel/security/lsm`). An explicit `lsm=` boot parameter must retain
Landlock alongside the other required LSMs. Container seccomp profiles must
allow `landlock_create_ruleset`, `landlock_add_rule`, and
`landlock_restrict_self`; changing the image's Ubuntu version cannot fix an
older or restricted host kernel. Landlock needs no privileged container or
extra capabilities. With custom/older seccomp profiles, add these three
syscalls to that profile rather than disabling seccomp.

Check support from the repository root, or `/app` inside the worker image:

```bash
python3 -c 'from worker.arcworker.tools.process import check_sandbox; check_sandbox()'
# In the worker image the package is named arcworker:
docker compose run --rm --no-deps --entrypoint python3 worker -c \
  'from arcworker.tools.process import check_sandbox; check_sandbox()'
```

Every external worker command runs with an explicit write policy: extraction
and conversion outputs are writable, stdout-only parsers/decompressors have
no writable output directory, and each invocation gets private temporary,
home and cache directories. `/dev/null` is writable; other device writes and
device/FIFO/socket creation are denied. Scratch is removed after the process
group is stopped. The worker itself retains the access needed for API/storage
operations and Python-managed output files.

This policy restricts file-content writes, truncation, creation, removal,
rename and link operations. Reads and program execution remain unrestricted;
network access and some metadata operations (such as chmod) are not covered.
Access through descriptors opened before enforcement is also not revoked.
Keep archive path checks, output sanitisation, timeouts and decompression
limits in place. In-process libraries are outside this subprocess policy.

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

# Uniqueness is scoped to the selected items by default. --global-check also
# requires launch targets to be unique across the entire catalogue.
arco hashdb generate-riscos --tag arcarc --db-name "Apps" --output apps.json --global-check

# Apps like !ArcFS / !System appear on many discs. Pin each to its "golden"
# source: dump an editable candidates file, edit it, then feed it back so the
# bundled copies are dropped (and the golden launch target becomes mandatory).
arco hashdb generate-riscos --item <uuid> --db-name "Apps" --dump-canonical canonical.txt
arco hashdb generate-riscos --item <uuid> --db-name "Apps" --output apps.json \
    --canonical-sources canonical.txt

# Byte-identical copies of an app bundled with several products (e.g. Equasor in
# Impression Publisher/Style and standalone) are merged into one product by
# default; --no-merge-duplicates keeps them separate.
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
