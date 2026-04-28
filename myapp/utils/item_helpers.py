"""Shared item and tag helpers used by web and API flows."""

from collections.abc import Iterable

from ..database import Item, Tag
from ..extensions import db


def item_choice_list(model, placeholder: str):
    """Build a standard select choice list from a named taxonomy model."""
    return [(0, placeholder)] + [
        (row.id, row.name) for row in model.query.order_by(model.name).all()
    ]


def indented_item_choices(*, value_fn=lambda item: item.id,
                          exclude_ids=None):
    """Build a hierarchically-indented choice list of all items.

    Args:
        value_fn: callable returning the choice value for each item
                  (default: item.id; use ``lambda i: i.url_id`` for UUID keys).
        exclude_ids: optional set of item IDs to omit from the list.

    Returns:
        List of ``(value, indented_name)`` tuples sorted by name.
    """
    all_items = Item.query.order_by(Item.name).all()
    id_to_item = {item.id: item for item in all_items}
    _exclude = exclude_ids or set()

    def _depth(item):
        d = 0
        current = item.parent_id
        while current is not None:
            d += 1
            parent = id_to_item.get(current)
            current = parent.parent_id if parent else None
        return d

    choices = []
    for item in all_items:
        if item.id in _exclude:
            continue
        indent = '\u00a0\u00a0\u00a0\u00a0' * _depth(item)
        choices.append((value_fn(item), f"{indent}{item.name}"))
    return choices


def item_parent_choice_list(placeholder: str, exclude_item=None):
    """Build a parent item select choice list, excluding an item and its descendants.

    exclude_item: the Item being edited (self + descendants are excluded to prevent cycles).
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

    return [(0, placeholder)] + indented_item_choices(exclude_ids=excluded_ids)


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
