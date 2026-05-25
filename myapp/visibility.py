"""Privacy visibility helpers.

A private item/artefact is visible only to administrators and to the user who
owns it.  Privacy descends strictly down the item hierarchy
(``Item.private_effective``); an artefact is private if its own ``is_private``
flag is set or its item is effectively private.

Two flavours of helper are provided:

* ``can_view_item`` / ``can_view_artefact`` — per-object boolean checks for use
  in route guards.
* ``item_visibility_clause`` / ``artefact_visibility_clause`` — SQLAlchemy
  filter clauses for list/search queries.  ``artefact_visibility_clause``
  assumes the query is JOINed to ``Item``.

``user`` is normally ``flask_login.current_user`` (web) or the ``User`` owning
an API key (or ``None`` for anonymous / the worker, in which case pass
``sees_all=True`` for the worker, which must process private content).
"""

from sqlalchemy import and_, or_, true
from .database import Artefact, Item


def _is_authenticated(user) -> bool:
    return bool(user is not None and getattr(user, 'is_authenticated', False))


def _is_admin(user) -> bool:
    return _is_authenticated(user) and bool(getattr(user, 'is_admin', False))


def _user_id(user):
    return user.id if _is_authenticated(user) else None


def can_view_item(item: Item, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* may view *item* (respecting privacy)."""
    if not item.private_effective:
        return True
    if sees_all or _is_admin(user):
        return True
    uid = _user_id(user)
    return uid is not None and item.owner_id == uid


def can_view_artefact(artefact: Artefact, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* may view *artefact* (respecting privacy)."""
    if not artefact.effective_private:
        return True
    if sees_all or _is_admin(user):
        return True
    uid = _user_id(user)
    if uid is None:
        return False
    return artefact.owner_id == uid or (artefact.item is not None and artefact.item.owner_id == uid)


def item_visibility_clause(user, *, sees_all: bool = False):
    """SQLAlchemy clause to filter an ``Item`` query by *user*'s visibility."""
    if sees_all or _is_admin(user):
        return true()
    uid = _user_id(user)
    if uid is None:
        return Item.private_effective.is_(False)
    return or_(Item.private_effective.is_(False), Item.owner_id == uid)


def artefact_visibility_clause(user, *, sees_all: bool = False):
    """SQLAlchemy clause to filter an ``Artefact`` query (JOINed to ``Item``)."""
    if sees_all or _is_admin(user):
        return true()
    public = and_(Artefact.is_private.is_(False), Item.private_effective.is_(False))
    uid = _user_id(user)
    if uid is None:
        return public
    return or_(public, Artefact.owner_id == uid, Item.owner_id == uid)

# vim: ts=4 sw=4 et
