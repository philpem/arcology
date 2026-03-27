import re
import click
from flask import current_app
from ..extensions import db
from ..database import Artefact, Item, Tag, Platform, Category
from ..blueprints.artefacts import (
    reset_artefact_for_reanalysis, queue_analyses_for_artefact,
    _cleanup_analysis_outputs, get_output_folder,
)
from shared.enums import ArtefactType


@click.command('reanalyse')
@click.option('--item', 'item_uuid', default=None,
              help='UUID of a single item (reanalyse only its artefacts)')
@click.option('--tag', 'tag_name', default=None,
              help='Only artefacts whose item has this tag')
@click.option('--platform', 'platform_name', default=None,
              help='Only artefacts whose item belongs to this platform')
@click.option('--category', 'category_name', default=None,
              help='Only artefacts whose item belongs to this category')
@click.option('--artefact-type', 'artefact_type_name', default=None,
              help='Only artefacts of this type (e.g. SCP, HFE, IMG)')
@click.option('--all', 'select_all', is_flag=True, default=False,
              help='Reanalyse every artefact in the database')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would be requeued without making changes')
@click.option('--batch-size', default=50, show_default=True,
              help='Number of artefacts to process per database commit')
def reanalyse(item_uuid, tag_name, platform_name, category_name,
              artefact_type_name, select_all, dry_run, batch_size):
    """Reset and re-queue analysis for artefacts in the database.

    At least one filter or --all is required. Filters (--item, --tag,
    --platform, --category, --artefact-type) can be combined; they are
    ANDed together.

    Examples:

      flask reanalyse --all --dry-run
      flask reanalyse --artefact-type SCP
      flask reanalyse --platform "Acorn Archimedes" --tag "needs-review"
      flask reanalyse --item abc123def456
    """
    has_filter = any([item_uuid, tag_name, platform_name, category_name, artefact_type_name])

    if not has_filter and not select_all:
        click.echo("ERROR: specify at least one filter (--item, --tag, --platform, "
                    "--category, --artefact-type) or use --all to reanalyse everything.", err=True)
        raise SystemExit(1)

    # Build query: root artefacts only (derived are cleaned up by reset)
    query = Artefact.query.filter(Artefact.parent_artefact_id.is_(None))

    if item_uuid or tag_name or platform_name or category_name:
        query = query.join(Item)

    if item_uuid:
        identifier = item_uuid
        if re.fullmatch(r'[0-9a-f]{32}', identifier):
            query = query.filter(Item.uuid == identifier)
        elif len(identifier) >= 8 and re.fullmatch(r'[0-9a-f]{8}', identifier[:8]):
            prefix = identifier[:8]
            query = query.filter(Item.uuid.startswith(prefix))
        else:
            click.echo(f"ERROR: '{identifier}' is not a valid item UUID or URL identifier.", err=True)
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
            click.echo(f"ERROR: unknown artefact type '{artefact_type_name}'. "
                       f"Valid types: {valid}", err=True)
            raise SystemExit(1)
        query = query.filter(Artefact.artefact_type == at)

    artefacts = query.all()

    if not artefacts:
        click.echo("No matching artefacts found.")
        return

    click.echo(f"{'[DRY RUN] ' if dry_run else ''}Found {len(artefacts)} artefact(s) to reanalyse.")

    if dry_run:
        for a in artefacts:
            click.echo(f"  {a.uuid}  {a.artefact_type.name:20s}  {a.label}")
        return

    output_folder = get_output_folder()
    processed = 0

    for i, artefact in enumerate(artefacts, 1):
        click.echo(f"  [{i}/{len(artefacts)}] {artefact.uuid}  {artefact.label}")
        cleanup = reset_artefact_for_reanalysis(artefact)
        queue_analyses_for_artefact(artefact)
        _cleanup_analysis_outputs(
            output_folder,
            cleanup['output_files'],
            cleanup['output_dirs'],
            cleanup['cache_dir'],
            current_app.logger,
        )
        processed += 1

        if processed % batch_size == 0:
            db.session.commit()

    db.session.commit()
    click.echo(f"Done. {processed} artefact(s) reset and requeued for analysis.")

# vim: ts=4 sw=4 et
