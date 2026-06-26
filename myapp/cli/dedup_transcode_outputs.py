import click
from ..services.transcode_dedup import dedup_transcode_outputs


@click.command('dedup-transcode-outputs')
@click.option('--dry-run', is_flag=True, default=False,
              help='Report what would change without modifying storage or the DB')
@click.option('--batch-size', default=200, show_default=True,
              help='Number of rows to commit per batch')
def dedup_transcode_outputs_cmd(dry_run, batch_size):
    """Collapse legacy duplicate media transcodes onto shared output blobs.

    New transcodes are content-addressed and deduplicated automatically.  This
    command migrates *legacy* transcode outputs (produced before content-
    addressing) onto the same shared, refcounted OutputBlobs -- WITHOUT
    re-running the transcode.  The source file's hash is already recorded, so the
    canonical output location is known; the one surviving copy per source is
    hashed to record its output hash, identical-source duplicates link to it, and
    the redundant copies are reclaimed.

    Rows whose source hash cannot be resolved unambiguously are skipped and
    counted (a later ``flask reanalyse`` migrates them via the normal path).

    Safe to re-run -- rows already linked to a blob are skipped.

    Examples:

      flask dedup-transcode-outputs --dry-run
      flask dedup-transcode-outputs
    """
    stats = dedup_transcode_outputs(dry_run=dry_run, batch_size=batch_size)
    prefix = '[dry-run] ' if dry_run else ''
    click.echo(
        f"{prefix}{stats.linked} row(s) linked, "
        f"{stats.blobs_created} canonical output(s) registered, "
        f"{stats.files_reclaimed} duplicate object(s) "
        f"{'would be ' if dry_run else ''}reclaimed, "
        f"{stats.skipped} skipped (source unresolved/missing)."
    )
    if dry_run and (stats.linked or stats.files_reclaimed):
        click.echo("Run again without --dry-run to apply.")

# vim: ts=4 sw=4 et
