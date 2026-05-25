"""Privacy inheritance helpers for items.

Privacy is set explicitly on an item (``is_private``) and strictly descends to
every sub-item and artefact within it.  ``Item.private_effective`` is a
denormalised boolean ("own flag OR any ancestor private") that list/search
queries can filter on cheaply.  These helpers keep that column in sync whenever
an item's own flag changes, its parent changes, or it is created.
"""

from ..database import Item


def compute_effective_private(item: Item) -> bool:
    """Effective privacy = own flag OR the parent's effective privacy."""
    parent_effective = item.parent.private_effective if item.parent else False
    return bool(item.is_private or parent_effective)


def recompute_item_privacy(item: Item, _parent_effective=None) -> None:
    """Recompute ``private_effective`` for *item* and all of its descendants.

    Call after creating an item, toggling ``is_private``, or moving an item to a
    new parent.  The caller is responsible for committing the session.
    """
    if _parent_effective is None:
        _parent_effective = item.parent.private_effective if item.parent else False
    item.private_effective = bool(item.is_private or _parent_effective)
    for child in item.children:
        recompute_item_privacy(child, item.private_effective)

# vim: ts=4 sw=4 et
