import click
from flask import current_app
from ..extensions import db
from ..database import Artefact
from ..blueprints.artefacts import (
    reset_artefact_for_reanalysis, queue_analyses_for_artefact,
    _cleanup_analysis_outputs, get_output_folder,
)
from ._selection import build_artefact_query


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
def reanalyse(item_uuid, tag_name, platform_name, category_name,
              artefact_type_name, select_all, dry_run):
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
    query = build_artefact_query(
        item_uuid=item_uuid,
        tag_name=tag_name,
        platform_name=platform_name,
        category_name=category_name,
        artefact_type_name=artefact_type_name,
        select_all=select_all,
        root_only=True,
    )

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
        # commit=False on reset, commit=True on queue: one commit per artefact
        # covers both the bulk deletes and the new analysis inserts.
        cleanup = reset_artefact_for_reanalysis(artefact, commit=False)
        queue_analyses_for_artefact(artefact, skip_duplicate_check=True, commit=True)
        _cleanup_analysis_outputs(
            output_folder,
            cleanup['output_files'],
            cleanup['output_dirs'],
            cleanup['cache_dir'],
            current_app.logger,
        )
        processed += 1

    click.echo(f"Done. {processed} artefact(s) reset and requeued for analysis.")

# vim: ts=4 sw=4 et
