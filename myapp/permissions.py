"""
Arcology - Web Route Permission Decorators

Provides require_permission() for enforcing user permission levels on web
blueprint routes, plus visibility guards (require_visible_item and the
ensure_* helpers) that make privacy enforcement declarative instead of
hand-rolled per route. Kept in a separate module to avoid circular imports
between app.py and the individual blueprints.
"""

from functools import wraps
from flask import abort, current_app
from flask_login import current_user
from .database import Item, UserPermission
from .utils.config import bool_config
from .utils.slugs import lookup_by_identifier
from .visibility import can_contribute_to_item, can_view_item


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
                    and not bool_config('PUBLIC_MODE')):
                from .extensions import login_manager
                return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper


def ensure_visible_item(item) -> None:
    """Abort 404 if the current user may not view *item*.

    404 (not 403) so that private items are indistinguishable from
    nonexistent ones — the existence-hiding convention used throughout the
    web blueprints.
    """
    if not can_view_item(item, current_user):
        abort(404)


def ensure_item_contribution(item) -> None:
    """Abort 403 if the current user may not add/modify content in *item*.

    Mirrors the long-standing route convention: within a private item an
    editor- or curator-level share (or owner/admin) is required; public items
    remain writable by any user who passed the route's permission gate.
    """
    if item.private_effective and not can_contribute_to_item(item, current_user):
        abort(403)


def require_visible_item(param: str = 'uuid', *, contribute: bool = False):
    """Decorator: resolve an Item route parameter and enforce its visibility.

    Looks up the item identified by the *param* route argument (slug or UUID,
    via lookup_by_identifier), aborts 404 if the current user may not view it,
    and — when ``contribute=True`` — aborts 403 if the user may not modify
    content within it (see ensure_item_contribution).

    The resolved item is passed to the view as an ``item`` keyword argument;
    the original route parameter is preserved so views can still canonicalise
    URLs::

        @blueprint.route('/<string:uuid>/edit', methods=['GET', 'POST'])
        @login_required
        @require_permission('read_write')
        @require_visible_item(contribute=True)
        def edit(uuid, item):
            ...

    Apply below the auth decorator (@login_required / @public_readable) so
    anonymous users are redirected to login before any lookup happens.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            item = lookup_by_identifier(Item, kwargs[param])
            ensure_visible_item(item)
            if contribute:
                ensure_item_contribution(item)
            kwargs['item'] = item
            return f(*args, **kwargs)
        return wrapper
    return decorator


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
            elif not (bool_config('PUBLIC_MODE') and bool_config('PUBLIC_DOWNLOADS', default=True)):
                from .extensions import login_manager
                return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper

# vim: ts=4 sw=4 et
