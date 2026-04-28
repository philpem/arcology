"""
Arcology - Web Route Permission Decorator

Provides require_permission() for enforcing user permission levels on web
blueprint routes. Kept in a separate module to avoid circular imports between
app.py and the individual blueprints.
"""

from functools import wraps

from flask import abort
from flask_login import current_user

from .database import UserPermission


def require_permission(level: str):
    """
    Decorator for web routes that enforces the current user's permission level.

    Must be applied AFTER @login_required so that current_user is guaranteed
    to be an authenticated User object.

    Usage::

        @blueprint.route('/items/new', methods=['GET', 'POST'])
        @login_required
        @require_permission('read_write')
        def new_item():
            ...

    Args:
        level: Minimum permission level required ('read_only' or 'read_write').
    """
    required = UserPermission(level)

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.has_permission(required):
                abort(403)
            return f(*args, **kwargs)
        return wrapper

    return decorator

# vim: ts=4 sw=4 et
