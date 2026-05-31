"""Shared item and tag helpers used by web and API flows."""

from collections.abc import Iterable
from ..database import Item, Tag
from ..extensions import db


def item_choice_list(model, placeholder: str):
    """Build a standard select choice list from a named taxonomy model."""
    return [(0, placeholder)] + [
        (row.id, row.name) for row in model.query.order_by(model.name).all()
    ]


def indented_taxonomy_choices(model, placeholder: str):
    """Build a hierarchically-indented select list for Platform or Category.

    Produces depth-first tree order with non-breaking-space indentation so a
    flat <select> element visually reflects the parent/child hierarchy.  The
    model must have ``id``, ``name``, ``parent_id``, and ``children`` fields.
    """
    all_rows = model.query.order_by(model.name).all()
    children_map: dict[int | None, list] = {}
    for row in all_rows:
        children_map.setdefault(row.parent_id, []).append(row)

    choices = [(0, placeholder)]

    def _traverse(parent_id, depth):
        indent = '    ' * depth
        for row in children_map.get(parent_id, []):
            choices.append((row.id, f"{indent}{row.name}"))
            _traverse(row.id, depth + 1)

    _traverse(None, 0)
    return choices


def indented_item_choices(*, value_fn=lambda item: item.id,
                          exclude_ids=None, viewer=None):
    """Build a hierarchically-indented choice list of all items.

    Items are returned in depth-first tree order (each parent immediately
    before its children, siblings sorted alphabetically), so the visual
    indentation in a flat select element always reflects the true hierarchy.

    Args:
        value_fn: callable returning the choice value for each item
                  (default: item.id; use ``lambda i: i.url_id`` for UUID keys).
        exclude_ids: optional set of item IDs to omit from the list.
                     Excluded items and their entire subtrees are skipped.
        viewer: optional user; when supplied, private items the viewer may not
                see are filtered out (along with their subtrees).

    Returns:
        List of ``(value, indented_name)`` tuples in tree traversal order.
    """
    query = Item.query
    if viewer is not None:
        from ..visibility import item_visibility_clause
        query = query.filter(item_visibility_clause(viewer))
    all_items = query.order_by(Item.name).all()
    _exclude = exclude_ids or set()

    children_by_parent: dict[int | None, list] = {}
    for item in all_items:
        children_by_parent.setdefault(item.parent_id, []).append(item)

    choices = []

    def _traverse(parent_id, depth):
        indent = '\u00a0\u00a0\u00a0\u00a0' * depth
        for item in children_by_parent.get(parent_id, []):
            if item.id in _exclude:
                continue
            choices.append((value_fn(item), f"{indent}{item.name}"))
            _traverse(item.id, depth + 1)

    _traverse(None, 0)
    return choices


def item_parent_choice_list(placeholder: str, exclude_item=None, viewer=None):
    """Build a parent item select choice list, excluding an item and its descendants.

    exclude_item: the Item being edited (self + descendants are excluded to prevent cycles).
    viewer: optional user; private items the viewer may not see are filtered out.
    Returns [(id, indented_name), ...] with indentation reflecting depth.
    """
    excluded_ids: set[int] = set()
    if exclude_item is not None:
        excluded_ids.add(exclude_item.id)
        queue = list(exclude_item.children)
        while queue:
            child = queue.pop()
            excluded_ids.add(child.id)
            queue.extend(child.children)

    return [(0, placeholder)] + indented_item_choices(exclude_ids=excluded_ids, viewer=viewer)


def parse_tag_names(raw_tags) -> list[str]:
    """Normalize tag input from comma-separated text or a list of names."""
    if not raw_tags:
        return []
    if isinstance(raw_tags, str):
        values = raw_tags.split(',')
    elif isinstance(raw_tags, Iterable):
        values = raw_tags
    else:
        return []
    return [str(tag).strip() for tag in values if str(tag).strip()]


def assign_item_fields(
    item: Item,
    *,
    name: str,
    description=None,
    platform_id=None,
    category_id=None,
    parent_id=None,
):
    """Copy the core editable item fields onto an Item model."""
    item.name = name
    item.description = description
    item.platform_id = platform_id
    item.category_id = category_id
    item.parent_id = parent_id


def assign_item_tags(item: Item, raw_tags):
    """Replace an item's tags from normalized text or iterable input."""
    item.tags.clear()
    for tag_name in parse_tag_names(raw_tags):
        tag = Tag.query.filter_by(name=tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            db.session.add(tag)
        item.tags.append(tag)
