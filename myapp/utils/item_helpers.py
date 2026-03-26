"""Shared item and tag helpers used by web and API flows."""

from collections.abc import Iterable

from ..extensions import db
from ..database import Item, Tag


def item_choice_list(model, placeholder: str):
    """Build a standard select choice list from a named taxonomy model."""
    return [(0, placeholder)] + [
        (row.id, row.name) for row in model.query.order_by(model.name).all()
    ]


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
):
    """Copy the core editable item fields onto an Item model."""
    item.name = name
    item.description = description
    item.platform_id = platform_id
    item.category_id = category_id


def assign_item_tags(item: Item, raw_tags):
    """Replace an item's tags from normalized text or iterable input."""
    item.tags.clear()
    for tag_name in parse_tag_names(raw_tags):
        tag = Tag.query.filter_by(name=tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            db.session.add(tag)
        item.tags.append(tag)
