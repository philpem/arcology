"""Privacy visibility helpers.

A private item/artefact is visible only to administrators and to the user who
owns it, or to users/groups explicitly granted access via ItemShare.  Privacy
descends strictly down the item hierarchy (``Item.private_effective``); an
artefact is private if its own ``is_private`` flag is set or its item is
effectively private.

Two flavours of helper are provided:

* ``can_view_item`` / ``can_view_artefact`` — per-object boolean checks for use
  in route guards.
* ``item_visibility_clause`` / ``artefact_visibility_clause`` — SQLAlchemy
  filter clauses for list/search queries.  ``artefact_visibility_clause``
  assumes the query is JOINed to ``Item``.

``user`` is normally ``flask_login.current_user`` (web) or the ``User`` owning
an API key (or ``None`` for anonymous / the worker, in which case pass
``sees_all=True`` for the worker, which must process private content).

Three share permission levels are supported:

* ``viewer``  — read-only access to the item tree.
* ``editor``  — can add and modify content (artefacts, child items, references)
  but cannot change privacy flags or manage shares.
* ``curator`` — trusted co-curator equivalent to the owner for most operations:
  can toggle privacy and manage shares.  Ownership transfer always requires the
  actual owner or an admin.
"""

from sqlalchemy import and_, or_, select, true
from .database import Artefact, Item, ItemShare, group_memberships

# Valid share permission levels in ascending order of privilege.
SHARE_PERMISSIONS = ('viewer', 'editor', 'curator')


def _is_authenticated(user) -> bool:
    return bool(user is not None and getattr(user, 'is_authenticated', False))


def _is_admin(user) -> bool:
    return _is_authenticated(user) and bool(getattr(user, 'is_admin', False))


def _user_id(user):
    return user.id if _is_authenticated(user) else None


def is_owner(obj, user) -> bool:
    """True if *user* owns *obj* (an Item or Artefact)."""
    uid = _user_id(user)
    return uid is not None and obj.owner_id == uid


def can_manage_privacy(obj, user) -> bool:
    """Who may toggle the privacy flag on *obj*.

    The owner and administrators always may.  An object with no owner is
    treated as claimable: any authenticated user may privatise it (and thereby
    become its owner — see the edit handlers).
    """
    return _is_admin(user) or is_owner(obj, user) or (
        obj.owner_id is None and _is_authenticated(user)
    )


def can_change_owner(obj, user) -> bool:
    """Who may reassign ownership of *obj*: the current owner or an admin."""
    return _is_admin(user) or is_owner(obj, user)


def can_claim_item(item: "Item", user) -> bool:
    """True if *user* may claim ownership of *item* by privatising it.

    Only valid for unowned items.  A curator share is a managed relationship,
    not ownership — holders are excluded so they cannot promote themselves to
    owner via the privacy toggle.
    """
    if not _is_authenticated(user):
        return False
    if item.owner_id is not None:
        return False
    if _is_admin(user):
        return True
    uid = _user_id(user)
    return not _has_share_access(item, uid, level='curator')


def can_manage_shares(item: Item, user) -> bool:
    """Who may add/remove shares on an item: the owner, an admin, or a curator."""
    return _is_admin(user) or is_owner(item, user) or _has_share_access(item, _user_id(user), level='curator')


def _share_permission_filter(*, level: str = 'viewer'):
    """SQLAlchemy filter fragment for the required share permission level.

    ``level`` is one of 'viewer', 'editor', 'curator':
    * 'viewer'  — any share permission passes (no filter).
    * 'editor'  — editor or curator shares pass.
    * 'curator' — only curator shares pass.
    """
    if level == 'viewer':
        return true()
    if level == 'editor':
        return ItemShare.permission.in_(('editor', 'curator'))
    # curator
    return ItemShare.permission == 'curator'


def _has_share_access(item: Item, uid, *, level: str = 'viewer') -> bool:
    """True if uid has a share of at least *level* on item or any of its ancestors."""
    if uid is None:
        return False
    from .extensions import db
    chain_ids = [item.id] + [a.id for a in item.ancestors]
    if ItemShare.query.filter(
        ItemShare.item_id.in_(chain_ids),
        ItemShare.user_id == uid,
        _share_permission_filter(level=level),
    ).first():
        return True
    group_ids = db.session.execute(
        select(group_memberships.c.group_id).where(group_memberships.c.user_id == uid)
    ).scalars().all()
    if not group_ids:
        return False
    return ItemShare.query.filter(
        ItemShare.item_id.in_(chain_ids),
        ItemShare.group_id.in_(group_ids),
        _share_permission_filter(level=level),
    ).first() is not None


def _accessible_via_share_subquery(uid: int):
    """Subquery returning item IDs accessible to uid via ItemShare grants.

    Uses a recursive CTE anchored on items shared with uid (directly or via
    group), then expands to all their descendants — mirroring the strict-
    descend semantics of private_effective.

    Not denormalised: evaluated at query time.  Efficient because the anchor
    set (shares for one user) is small and the tree depth is bounded.
    """
    direct = select(ItemShare.item_id.label('id')).where(ItemShare.user_id == uid)
    via_group = (
        select(ItemShare.item_id.label('id'))
        .join(group_memberships, group_memberships.c.group_id == ItemShare.group_id)
        .where(ItemShare.group_id.is_not(None))
        .where(group_memberships.c.user_id == uid)
    )
    # Wrap the two anchor queries in a subquery so the CTE base is a plain
    # SELECT (not a compound SELECT) — required by SQLAlchemy's CTE API.
    anchor_sq = direct.union_all(via_group).subquery('_share_anchor')
    anchor = select(anchor_sq.c.id)
    cte = anchor.cte(name='accessible_via_share', recursive=True)
    recursive_step = (
        select(Item.id.label('id'))
        .join(cte, Item.parent_id == cte.c.id)
    )
    cte = cte.union_all(recursive_step)
    return select(cte.c.id)


def can_view_item(item: Item, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* may view *item* (respecting privacy)."""
    if not item.private_effective:
        return True
    if sees_all or _is_admin(user):
        return True
    uid = _user_id(user)
    if uid is None:
        return False
    if item.owner_id == uid:
        return True
    return _has_share_access(item, uid, level='viewer')


def can_contribute_to_item(item: Item, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* may add or modify content within *item*.

    Requires an editor- or curator-level share (or owner/admin).
    """
    if sees_all or _is_admin(user) or is_owner(item, user):
        return True
    uid = _user_id(user)
    if uid is None:
        return False
    return item.private_effective and _has_share_access(item, uid, level='editor')


def can_curate_item(item: Item, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* has curator-level access to *item*.

    Curators may toggle privacy and manage shares, mirroring the owner for all
    operations except transferring ownership (which always requires the actual
    owner or an admin).
    """
    if sees_all or _is_admin(user) or is_owner(item, user):
        return True
    uid = _user_id(user)
    if uid is None:
        return False
    return item.private_effective and _has_share_access(item, uid, level='curator')


def can_view_artefact(artefact: Artefact, user, *, sees_all: bool = False) -> bool:
    """Return True if *user* may view *artefact* (respecting privacy)."""
    if not artefact.effective_private:
        return True
    if sees_all or _is_admin(user):
        return True
    uid = _user_id(user)
    if uid is None:
        return False
    # When the enclosing item is private, item membership governs access.
    # Artefact ownership alone is not enough — the user must have a share or
    # own the item, otherwise revoked shares could be bypassed by artefact ownership.
    if artefact.item is not None and artefact.item.private_effective:
        return (artefact.item.owner_id == uid) or _has_share_access(artefact.item, uid, level='viewer')
    # Standalone artefact or artefact in a public item: artefact/item ownership governs.
    return artefact.owner_id == uid or (artefact.item is not None and artefact.item.owner_id == uid)


def can_download_despite_restrictions(user, restrictions, artefact) -> bool:
    """Single source of truth for whether a caller may download restricted content.

    Used by both the web download routes and the REST API so the two paths
    cannot drift apart.  Download restrictions are an access gate distinct from
    privacy/visibility: unlike private content (which the worker may read to
    analyse), restricted bytes are released only to a user who holds a bypass.

    * No restrictions → always allowed.
    * Anonymous, or a non-user caller such as the worker key (``user`` is None
      or not authenticated) → blocked.
    * Otherwise the user must be able to bypass *every* restriction — via a
      global per-type bypass or a per-artefact grant on ``artefact`` or any of
      its ancestors, so a grant on an original upload cascades to the artefacts
      derived from it.

    ``restrictions`` is any iterable of objects exposing ``.restriction_type``
    (``ArtefactRestriction`` or ``ExtractedFileRestriction``).  ``artefact`` is
    the artefact whose ancestor chain scopes per-artefact grants — for a file
    download pass the file's owning artefact (``ef.partition.artefact``).
    """
    if not restrictions:
        return True
    if not _is_authenticated(user):
        return False
    return user.can_bypass_all_restrictions(restrictions, artefact_id=artefact.ancestor_ids)


def output_blocked_for(user, artefact) -> bool:
    """Whether *user* is barred from an artefact's analysis OUTPUTS.

    Analysis outputs (image renders, text conversions, visualisations) are a
    rendering of the artefact's restricted bytes, so they are gated by the same
    download restrictions.  Returns ``True`` when *artefact* — or any ancestor
    it was derived from — carries a restriction *user* cannot bypass.
    Encapsulates the ``can_download_despite_restrictions`` inversion shared by
    the output-serving routes and the viewer.
    """
    return not can_download_despite_restrictions(
        user, artefact.effective_restrictions, artefact)


def item_visibility_clause(user, *, sees_all: bool = False):
    """SQLAlchemy clause to filter an ``Item`` query by *user*'s visibility."""
    if sees_all or _is_admin(user):
        return true()
    uid = _user_id(user)
    if uid is None:
        return Item.private_effective.is_(False)
    return or_(
        Item.private_effective.is_(False),
        Item.owner_id == uid,
        Item.id.in_(_accessible_via_share_subquery(uid)),
    )


def artefact_visibility_clause(user, *, sees_all: bool = False):
    """SQLAlchemy clause to filter an ``Artefact`` query (JOINed to ``Item``)."""
    if sees_all or _is_admin(user):
        return true()
    public = and_(Artefact.is_private.is_(False), Item.private_effective.is_(False))
    uid = _user_id(user)
    if uid is None:
        return public
    return or_(
        public,
        Item.owner_id == uid,
        and_(Item.private_effective.is_(True), Item.id.in_(_accessible_via_share_subquery(uid))),
        # Artefact owner sees their own privately-flagged artefact only when the
        # enclosing item is public; item privacy takes precedence otherwise.
        and_(Item.private_effective.is_(False), Artefact.owner_id == uid),
    )

# vim: ts=4 sw=4 et
