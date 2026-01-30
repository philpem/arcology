# Arcology

A digital artefact catalogue for retrocomputing collections, built on Flask.

## Features

- **Catalogue items** with multiple digital artefacts (disk images, scans, etc.)
- **Link to external systems** (Koillection, Collective Access, etc.)
- **Browse extracted file listings** from disk images
- **Identify known files** using hash databases
- **Queue analysis jobs** for automated processing
- **REST API** for integration with tools
- **User authentication** with Flask-Login

## Quick Start

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp myapp/myapp.cfg.example myapp/myapp.cfg
# Edit myapp/myapp.cfg - set SECRET_KEY and database path

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
│   │   ├── artefacts.py    # Artefact management
│   │   ├── taxonomy.py     # Platforms, categories, tags
│   │   ├── analysis.py     # Analysis queue
│   │   └── api.py          # REST API
│   ├── templates/          # Jinja2 templates
│   └── static/             # CSS, JS, images
├── requirements.txt
└── README.md
```

## Adding Features

Features are implemented as blueprints in `myapp/blueprints/`. Each blueprint:

1. Defines a `blueprint` variable
2. Optionally defines `init_app(app)` to register menu items
3. Is auto-loaded by the application factory

Example blueprint structure:
```python
from flask import Blueprint
blueprint = Blueprint('myfeature', __name__, url_prefix='/myfeature')

def init_app(app):
    app.add_menu_item("My Feature", "myfeature.index", 500)

@blueprint.route('/')
def index():
    return "Hello"
```

## API Endpoints

- `GET/POST /api/items` - List/create items
- `GET/PUT/DELETE /api/items/{id}` - Item operations
- `POST /api/items/{id}/artefacts` - Add artefact
- `GET /api/artefacts/{id}/download` - Download file
- `POST /api/artefacts/{id}/analysis` - Queue analysis
- `GET /api/analysis/pending` - Get pending jobs (for worker)
- `PUT /api/analysis/{id}` - Update analysis result

## License

MIT
