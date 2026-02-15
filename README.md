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
mkdir -p data/uploads data/outputs data/db

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

### Production Configuration

Create a `.env` file for production:

```bash
# Generate a secret key
echo "FLASK_ENV=production" > .env
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
```

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
FLASK_ENV=production              # production or development
SECRET_KEY=<your-secret-key>      # Generate with: python3 -c 'import secrets; print(secrets.token_hex(32))'

# Worker configuration (usually auto-configured in Docker)
ARCOLOGY_API=http://web:5000/api  # API endpoint URL
UPLOAD_DIR=/data/uploads          # Uploaded files directory
OUTPUT_DIR=/data/outputs          # Analysis outputs directory
POLL_INTERVAL=30                  # How often worker checks for jobs (seconds)
LOG_LEVEL=INFO                    # Logging level: DEBUG, INFO, WARNING, ERROR
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

# Initialize database
flask db init
flask db migrate -m "Initial"
flask db upgrade

# Create admin user (use Flask shell)
flask shell
>>> from myapp.database import User, db
>>> u = User(username='admin')
>>> u.setPassword('changeme')
>>> db.session.add(u)
>>> db.session.commit()
>>> exit()

# Run development server
python -m myapp
```

Visit http://localhost:5000

## Project Structure

```
arcology/
├── myapp/
│   ├── app.py              # Application factory
│   ├── database.py         # SQLAlchemy models
│   ├── extensions.py       # Flask extensions
│   ├── myapp.cfg           # Configuration
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

- `GET/POST /api/items` - List/create items
- `GET/PUT/DELETE /api/items/{id}` - Item operations
- `POST /api/items/{id}/artefacts` - Add artefact
- `GET /api/artefacts/{id}/download` - Download file
- `GET /api/outputs/{filename}` - Get analysis output (visualisation, etc.)
- `POST /api/artefacts/{id}/analysis` - Queue analysis
- `GET /api/analysis/pending` - Get pending jobs (for worker)
- `PUT /api/analysis/{id}` - Update analysis result

## License

MIT
