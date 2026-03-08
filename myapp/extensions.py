"""
Flask extensions initialization.

Extensions are created here without being bound to a specific application instance.
They are initialized with the app in the application factory (create_app).
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bootstrap import Bootstrap5
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bootstrap = Bootstrap5()
csrf = CSRFProtect()

# vim: ts=4 sw=4 et
