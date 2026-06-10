import json
import click
from flask import current_app
from ..database import Analysis, AnalysisStatus, Artefact
from ..extensions import db
from ..services.artefact_lifecycle import (
    bulk_delete_artefact_dependents,
    bulk_delete_artefacts,
    cleanup_analysis_outputs,
    delete_artefact_files,
    get_all_derived_artefact_ids,
    reset_artefact_for_reanalysis,
)
from ..services.artefact_storage import get_output_folder
from ..services.artefact_types import queue_analyses_for_artefact
from ._selection import build_artefact_query


@click.command('reanalyse')
@click.option('--analysis', 'analysis_uuid', default=None,
              help='UUID of a single analysis to retry (resets only that analysis and its output)')
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
def reanalyse(analysis_uuid, item_uuid, tag_name, platform_name, category_name,
              artefact_type_name, select_all, dry_run):
    """Reset and re-queue analysis for artefacts in the database.

    Use --analysis <uuid> to retry a single analysis without disturbing other
    completed work on the same artefact.  All other options reset the entire
    artefact (all analyses, derived artefacts, partitions and extracted files).

    At least one filter or --all is required for artefact-level reanalysis.
    Filters (--item, --tag, --platform, --category, --artefact-type) can be
    combined; they are ANDed together.

    Examples:

      flask reanalyse --analysis abc123def456
      flask reanalyse --all --dry-run
      flask reanalyse --artefact-type SCP
      flask reanalyse --platform "Acorn Archimedes" --tag "needs-review"
      flask reanalyse --item abc123def456
    """
    # -----------------------------------------------------------------------
    # Single analysis retry
    # -----------------------------------------------------------------------
    if analysis_uuid:
        analysis = Analysis.query.filter_by(uuid=analysis_uuid).first()
        if not analysis:
            click.echo(f"ERROR: analysis '{analysis_uuid}' not found.", err=True)
            raise SystemExit(1)

        artefact = analysis.artefact
        analysis_type = analysis.analysis_type
        hints = analysis.hints

        click.echo(
            f"{'[DRY RUN] ' if dry_run else ''}Retrying analysis {analysis_uuid} "
            f"({analysis_type.name}) on artefact {artefact.uuid} ({artefact.label})."
        )

        # Collect produced artefacts (direct + nested descendants via CTE).
        produced_direct = Artefact.query.filter_by(derived_from_analysis_id=analysis.id).all()
        all_produced_ids = []
        for pa in produced_direct:
            all_produced_ids.append(pa.id)
            all_produced_ids.extend(get_all_derived_artefact_ids(pa))

        if all_produced_ids:
            click.echo(f"  Will delete {len(all_produced_ids)} produced artefact(s).")

        if dry_run:
            return

        # Collect output paths before deletion.
        output_files = []
        output_dirs = []
        if analysis.output_path:
            output_dirs.append(analysis.output_path)
        if analysis.details:
            try:
                details = json.loads(analysis.details)
                if isinstance(details.get('outputs'), list):
                    for out in details['outputs']:
                        if 'filename' in out:
                            output_files.append(out['filename'])
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Delete storage files for all produced artefacts before DB cleanup.
        for pa in produced_direct:
            delete_artefact_files(pa)

        if all_produced_ids:
            # Null FK back-references before deleting analyses on produced artefacts.
            Artefact.query.filter(Artefact.id.in_(all_produced_ids)).update(
                {Artefact.derived_from_analysis_id: None}, synchronize_session=False)
            bulk_delete_artefact_dependents(all_produced_ids)
            bulk_delete_artefacts(all_produced_ids)

        db.session.delete(analysis)

        # Queue a fresh analysis of the same type/hints.
        new_analysis = Analysis(
            artefact_id=artefact.id,
            analysis_type=analysis_type,
            status=AnalysisStatus.PENDING,
            hints=hints,
        )
        db.session.add(new_analysis)
        db.session.commit()

        # Clean up output files from the old analysis.
        output_folder = get_output_folder()
        cache_dir = ''  # no cache to clear for a single analysis
        cleanup_analysis_outputs(
            output_folder, output_files, output_dirs, cache_dir, current_app.logger,
        )

        click.echo(f"Done. Analysis requeued as {new_analysis.uuid}.")
        return

    # -----------------------------------------------------------------------
    # Artefact-level reanalysis (existing behaviour)
    # -----------------------------------------------------------------------
    has_filter = any([item_uuid, tag_name, platform_name, category_name, artefact_type_name])

    if not has_filter and not select_all:
        click.echo("ERROR: specify --analysis <uuid>, at least one filter (--item, --tag, "
                    "--platform, --category, --artefact-type), or use --all to reanalyse "
                    "everything.", err=True)
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
        cleanup_analysis_outputs(
            output_folder,
            cleanup['output_files'],
            cleanup['output_dirs'],
            cleanup['cache_dir'],
            current_app.logger,
        )
        processed += 1

    click.echo(f"Done. {processed} artefact(s) reset and requeued for analysis.")

# vim: ts=4 sw=4 et
