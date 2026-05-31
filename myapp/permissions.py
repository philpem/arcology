"""
Arcology - Web Route Permission Decorator

Provides require_permission() for enforcing user permission levels on web
blueprint routes. Kept in a separate module to avoid circular imports between
app.py and the individual blueprints.
"""

from functools import wraps
from flask import abort, current_app
from flask_login import current_user
from .database import UserPermission


def _bool_config(key: str, default: bool = False) -> bool:
    v = current_app.config.get(key, default)
    if isinstance(v, str):
        return v.lower() in ('1', 'true', 'yes')
    return bool(v)


def require_permission(level: str):
    """
    Decorator for web routes that enforces the current user's permission level.

    Intended for use after @login_required or @public_readable.  As a
    belt-and-braces measure it also handles unauthenticated callers: anonymous
    users are always denied with 401 rather than crashing, so the decorator is
    safe even if it is accidentally applied without a prior auth gate.

    Usage::

        @blueprint.route('/items/new', methods=['GET', 'POST'])
        @login_required
        @require_permission('read_write')
        def new_item():
            ...

    Args:
        level: Minimum permission level required ('read_only', 'read_write', or 'staff').
    """
    required = UserPermission(level)

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                from .extensions import login_manager
                return login_manager.unauthorized()
            if not (getattr(current_user, 'is_admin', False) or current_user.has_permission(required)):
                abort(403)
            return f(*args, **kwargs)
        return wrapper

    return decorator


def public_readable(f):
    """Allow anonymous GET access when PUBLIC_MODE is enabled.

    When PUBLIC_MODE is off this behaves identically to @login_required,
    including honouring Flask-Login's LOGIN_DISABLED test flag.
    Apply this decorator in place of @login_required on read-only routes
    (list/detail views, search) in the dashboard, items, artefacts, and
    search blueprints.

    Usage::

        @blueprint.route('/items/')
        @public_readable
        def index():
            ...
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            if (not current_app.config.get('LOGIN_DISABLED', False)
                    and not _bool_config('PUBLIC_MODE')):
                from .extensions import login_manager
                return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper


def public_downloadable(f):
    """Allow anonymous downloads when PUBLIC_MODE and PUBLIC_DOWNLOADS are both enabled.

    PUBLIC_DOWNLOADS defaults to True so that enabling PUBLIC_MODE alone is
    sufficient for full read-only access including file downloads.  Set
    PUBLIC_DOWNLOADS = False to restrict anonymous visitors to metadata
    browsing only.

    Apply this decorator in place of @login_required on download/stream
    endpoints in the artefacts blueprint.  Respects LOGIN_DISABLED for tests.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            if current_app.config.get('LOGIN_DISABLED', False):
                pass
            elif not (_bool_config('PUBLIC_MODE') and _bool_config('PUBLIC_DOWNLOADS', default=True)):
                from .extensions import login_manager
                return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper

# vim: ts=4 sw=4 et
