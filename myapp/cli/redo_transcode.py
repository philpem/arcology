import click
from ..database import (
    ANALYSIS_PRIORITY_NORMAL,
    Analysis,
    AnalysisStatus,
    Artefact,
)
from ..extensions import db
from ..services.transcode_dedup import (
    invalidate_transcodes,
    requeue_targets,
    source_hashes_for_artefact,
)


@click.command('redo-transcode')
@click.option('--artefact', 'artefact_uuid', default=None,
              help='Redo every transcode owned by this artefact (UUID)')
@click.option('--source-hash', 'source_hash', default=None,
              help='Redo the transcode of this source media SHA-256 (all '
                   'artefacts sharing it)')
@click.option('--no-reanalyse', is_flag=True, default=False,
              help='Only invalidate the cached output; do not re-queue encoding')
@click.option('--dry-run', is_flag=True, default=False,
              help='Report what would change without modifying anything')
def redo_transcode(artefact_uuid, source_hash, no_reanalyse, dry_run):
    """Discard a bad transcode and re-encode it from scratch.

    Media transcodes are content-addressed and cached on the SOURCE file's hash,
    so a plain ``flask reanalyse`` is a cache *hit* and re-serves the same bad
    output.  This command first INVALIDATES the cached output (deletes the shared
    OutputBlob(s) and files, clears every referencing row), then re-queues the
    transcode so the worker re-encodes fresh.  A bad transcode of a source is bad
    for every artefact sharing that source, so invalidation is scoped by source.

    Specify the target by artefact or by source hash (at least one):

      flask redo-transcode --artefact 1a2b3c... --dry-run
      flask redo-transcode --artefact 1a2b3c...
      flask redo-transcode --source-hash deadbeef... --no-reanalyse
    """
    if not artefact_uuid and not source_hash:
        click.echo("ERROR: specify --artefact or --source-hash.", err=True)
        raise SystemExit(1)

    source_hashes = set()
    if source_hash:
        source_hashes.add(source_hash.lower())
    if artefact_uuid:
        artefact = Artefact.query.filter_by(uuid=artefact_uuid).first()
        if not artefact:
            click.echo(f"ERROR: artefact '{artefact_uuid}' not found.", err=True)
            raise SystemExit(1)
        source_hashes |= source_hashes_for_artefact(artefact.id)

    if not source_hashes:
        click.echo("No transcodes found for the given target.")
        return

    # Resolve re-queue targets BEFORE invalidation clears the references.
    targets = requeue_targets(source_hashes) if not no_reanalyse else set()

    counts = invalidate_transcodes(source_hashes, dry_run=dry_run)
    prefix = '[dry-run] ' if dry_run else ''
    click.echo(
        f"{prefix}{len(source_hashes)} source(s): "
        f"{counts['blobs']} blob(s), {counts['objects']} object(s), "
        f"{counts['rows']} row(s) "
        f"{'would be ' if dry_run else ''}invalidated."
    )

    if no_reanalyse:
        click.echo("Re-analysis skipped (--no-reanalyse); cache is cleared.")
        return

    queued = 0
    for artefact_id, analysis_type in sorted(targets, key=lambda t: (t[0], t[1].name)):
        if dry_run:
            queued += 1
            continue
        existing = Analysis.query.filter_by(
            artefact_id=artefact_id, analysis_type=analysis_type
        ).filter(
            Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING])
        ).first()
        if existing:
            continue
        db.session.add(Analysis(
            artefact_id=artefact_id,
            analysis_type=analysis_type,
            status=AnalysisStatus.PENDING,
            priority=ANALYSIS_PRIORITY_NORMAL,
        ))
        queued += 1
    if not dry_run:
        db.session.commit()
    click.echo(
        f"{prefix}{queued} transcode analysis job(s) "
        f"{'would be ' if dry_run else ''}re-queued."
    )

# vim: ts=4 sw=4 et
