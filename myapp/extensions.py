"""
Flask extensions initialization.

Extensions are created here without being bound to a specific application instance.
They are initialized with the app in the application factory (create_app).
"""

from flask_bootstrap import Bootstrap5
from flask_caching import Cache
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bootstrap = Bootstrap5()
csrf = CSRFProtect()
# Read-through cache. Degrades to NullCache (no caching, always fresh) unless a
# backend is configured — see create_app() and myapp/services/cache.py.
cache = Cache()

# vim: ts=4 sw=4 et
