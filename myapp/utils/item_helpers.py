"""Shared item and tag helpers used by web and API flows."""

from collections.abc import Iterable

from ..extensions import db
from ..database import Item, Tag


def item_choice_list(model, placeholder: str):
    """Build a standard select choice list from a named taxonomy model."""
    return [(0, placeholder)] + [
        (row.id, row.name) for row in model.query.order_by(model.name).all()
    ]


def item_parent_choice_list(placeholder: str, exclude_item=None):
    """Build a parent item select choice list, excluding an item and its descendants.

    exclude_item: the Item being edited (self + descendants are excluded to prevent cycles).
    Returns [(id, indented_name), ...] with indentation reflecting depth.
    """
    from ..database import Item

    all_items = Item.query.order_by(Item.name).all()

    # Build set of IDs to exclude (the item itself and all its descendants)
    excluded_ids: set[int] = set()
    if exclude_item is not None:
        excluded_ids.add(exclude_item.id)
        # Collect all descendant IDs via BFS
        queue = list(exclude_item.children)
        while queue:
            child = queue.pop()
            excluded_ids.add(child.id)
            queue.extend(child.children)

    # Build a depth map so we can indent choices
    id_to_item = {item.id: item for item in all_items}
    def depth(item):
        d = 0
        current = item.parent_id
        while current is not None:
            d += 1
            parent = id_to_item.get(current)
            current = parent.parent_id if parent else None
        return d

    choices = [(0, placeholder)]
    for item in all_items:
        if item.id in excluded_ids:
            continue
        indent = '\u00a0\u00a0\u00a0\u00a0' * depth(item)  # non-breaking spaces for indent
        choices.append((item.id, f"{indent}{item.name}"))
    return choices


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
