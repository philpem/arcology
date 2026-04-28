import click
from ..database import Analysis, AnalysisStatus, Artefact
from ..extensions import db
from ._selection import build_artefact_query


@click.command('cancel-analysis')
@click.option('--analysis', 'analysis_uuid', default=None,
              help='UUID of a single analysis to cancel')
@click.option('--artefact', 'artefact_uuid', default=None,
              help='UUID of a single artefact (cancel all its pending analyses)')
@click.option('--item', 'item_uuid', default=None,
              help='UUID of a single item (cancel pending analyses for all its artefacts)')
@click.option('--tag', 'tag_name', default=None,
              help='Only artefacts whose item has this tag')
@click.option('--platform', 'platform_name', default=None,
              help='Only artefacts whose item belongs to this platform')
@click.option('--category', 'category_name', default=None,
              help='Only artefacts whose item belongs to this category')
@click.option('--artefact-type', 'artefact_type_name', default=None,
              help='Only artefacts of this type (e.g. SCP, HFE, IMG)')
@click.option('--all', 'select_all', is_flag=True, default=False,
              help='Cancel all pending analyses in the database')
@click.option('--include-running', is_flag=True, default=False,
              help='Also cancel RUNNING analyses (the worker may still complete them)')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would be cancelled without making changes')
def cancel_analysis(analysis_uuid, artefact_uuid, item_uuid, tag_name, platform_name,
                    category_name, artefact_type_name, select_all, include_running, dry_run):
    """Cancel pending analyses without resetting artefact data.

    Cancels PENDING analyses by default; add --include-running to also
    cancel analyses already claimed by a worker (the worker will finish
    processing but its update will be silently discarded).

    Exactly one selection method is required:

      --analysis <uuid>   cancel a single analysis by UUID
      --artefact <uuid>   cancel all pending analyses on one artefact
      --item / --tag / --platform / --category / --artefact-type
                          cancel pending analyses matching artefact filters
      --all               cancel every pending analysis in the database

    Item-level filters (--item, --tag, --platform, --category,
    --artefact-type) can be combined and are ANDed together.  They cancel
    analyses on all matching artefacts, including derived ones.

    Examples:

      flask cancel-analysis --all --dry-run
      flask cancel-analysis --analysis abc123def456
      flask cancel-analysis --artefact def456abc123
      flask cancel-analysis --item abc123 --dry-run
      flask cancel-analysis --platform "Acorn Archimedes"
      flask cancel-analysis --all --include-running
    """
    prefix = "[DRY RUN] " if dry_run else ""
    statuses = [AnalysisStatus.PENDING]
    if include_running:
        statuses.append(AnalysisStatus.RUNNING)
    status_label = '/'.join(s.name for s in statuses)

    # -----------------------------------------------------------------------
    # Single analysis by UUID
    # -----------------------------------------------------------------------
    if analysis_uuid:
        analysis = Analysis.query.filter_by(uuid=analysis_uuid).first()
        if not analysis:
            click.echo(f"ERROR: analysis '{analysis_uuid}' not found.", err=True)
            raise SystemExit(1)
        if analysis.status not in statuses:
            click.echo(
                f"ERROR: analysis {analysis_uuid} has status "
                f"{analysis.status.name} — only {status_label} analyses can be "
                f"cancelled (add --include-running to also cancel RUNNING).",
                err=True,
            )
            raise SystemExit(1)
        if dry_run:
            click.echo(
                f"{prefix}Would cancel analysis {analysis_uuid} "
                f"({analysis.analysis_type.name}, {analysis.status.name})."
            )
            return
        db.session.delete(analysis)
        db.session.commit()
        click.echo(f"Cancelled analysis {analysis_uuid} ({analysis.analysis_type.name}).")
        return

    # -----------------------------------------------------------------------
    # Single artefact by UUID
    # -----------------------------------------------------------------------
    if artefact_uuid:
        artefact = Artefact.query.filter_by(uuid=artefact_uuid).first()
        if not artefact:
            click.echo(f"ERROR: artefact '{artefact_uuid}' not found.", err=True)
            raise SystemExit(1)
        analyses = (
            Analysis.query
            .filter(
                Analysis.artefact_id == artefact.id,
                Analysis.status.in_(statuses),
            )
            .all()
        )
        if not analyses:
            click.echo(
                f"No {status_label} analyses found for artefact {artefact_uuid}."
            )
            return
        click.echo(
            f"{prefix}Found {len(analyses)} {status_label} analysis/analyses "
            f"on artefact {artefact_uuid} ({artefact.label})."
        )
        if dry_run:
            for a in analyses:
                click.echo(f"  {a.uuid}  {a.analysis_type.name:30s}  {a.status.name}")
            return
        for a in analyses:
            db.session.delete(a)
        db.session.commit()
        click.echo(f"Done. Cancelled {len(analyses)} analysis/analyses.")
        return

    # -----------------------------------------------------------------------
    # Item-level filters (--item, --tag, --platform, --category,
    # --artefact-type, --all)
    # -----------------------------------------------------------------------
    has_filter = any([item_uuid, tag_name, platform_name, category_name, artefact_type_name])

    if not has_filter and not select_all:
        click.echo(
            "ERROR: specify --analysis, --artefact, at least one of "
            "(--item, --tag, --platform, --category, --artefact-type), "
            "or use --all.",
            err=True,
        )
        raise SystemExit(1)

    # Include derived artefacts: cancel covers the whole artefact tree for
    # each matched item, unlike reanalyse which only processes root artefacts.
    query = build_artefact_query(
        item_uuid=item_uuid,
        tag_name=tag_name,
        platform_name=platform_name,
        category_name=category_name,
        artefact_type_name=artefact_type_name,
        select_all=select_all,
        root_only=False,
    )

    artefacts = query.all()
    if not artefacts:
        click.echo("No matching artefacts found.")
        return

    artefact_ids = [a.id for a in artefacts]
    analyses = (
        Analysis.query
        .filter(
            Analysis.artefact_id.in_(artefact_ids),
            Analysis.status.in_(statuses),
        )
        .all()
    )

    if not analyses:
        click.echo(
            f"No {status_label} analyses found across "
            f"{len(artefacts)} matching artefact(s)."
        )
        return

    click.echo(
        f"{prefix}Found {len(analyses)} {status_label} analysis/analyses "
        f"across {len(artefacts)} artefact(s)."
    )

    if dry_run:
        for a in analyses:
            click.echo(
                f"  {a.uuid}  {a.analysis_type.name:30s}  {a.status.name}"
                f"  artefact={a.artefact.uuid}"
            )
        return

    for a in analyses:
        db.session.delete(a)
    db.session.commit()
    click.echo(f"Done. Cancelled {len(analyses)} analysis/analyses.")

# vim: ts=4 sw=4 et
