"""Shared artefact selection helpers for CLI commands."""

import re

import click

from shared.enums import ArtefactType

from ..database import Artefact, Category, Item, Platform, Tag


def build_artefact_query(item_uuid=None, tag_name=None, platform_name=None,
                         category_name=None, artefact_type_name=None,
                         select_all=False, root_only=True):
    """Build an Artefact SQLAlchemy query from the standard filter options.

    Returns the query object.  Raises SystemExit(1) on invalid input.

    The caller is responsible for verifying that at least one filter or
    select_all=True was supplied before calling this function.

    Parameters
    ----------
    root_only : bool
        When True (default), restrict to root artefacts only
        (parent_artefact_id IS NULL).  Set to False to include derived
        artefacts as well.
    """
    if root_only:
        query = Artefact.query.filter(Artefact.parent_artefact_id.is_(None))
    else:
        query = Artefact.query

    if item_uuid or tag_name or platform_name or category_name:
        query = query.join(Item)

    if item_uuid:
        if re.fullmatch(r'[0-9a-f]{32}', item_uuid):
            query = query.filter(Item.uuid == item_uuid)
        elif len(item_uuid) >= 8 and re.fullmatch(r'[0-9a-f]{8}', item_uuid[:8]):
            query = query.filter(Item.uuid.startswith(item_uuid[:8]))
        else:
            click.echo(
                f"ERROR: '{item_uuid}' is not a valid item UUID or URL identifier.",
                err=True,
            )
            raise SystemExit(1)

    if tag_name:
        query = query.filter(Item.tags.any(Tag.name == tag_name))

    if platform_name:
        query = query.join(Platform).filter(Platform.name == platform_name)

    if category_name:
        query = query.join(Category).filter(Category.name == category_name)

    if artefact_type_name:
        try:
            at = ArtefactType[artefact_type_name]
        except KeyError:
            valid = ', '.join(t.name for t in ArtefactType)
            click.echo(
                f"ERROR: unknown artefact type '{artefact_type_name}'. "
                f"Valid types: {valid}",
                err=True,
            )
            raise SystemExit(1) from None
        query = query.filter(Artefact.artefact_type == at)

    return query

# vim: ts=4 sw=4 et
