import click
from ..database import Partition
from ..extensions import db
from ..services.hash_rescan import _refresh_partition_unique_counts


@click.command('reconcile-counts')
@click.option('--batch-size', default=500, show_default=True,
              help='Number of partitions to recompute per commit')
@click.option('--dry-run', is_flag=True, default=False,
              help='Report partitions whose stored counts differ, without writing')
def reconcile_counts(batch_size, dry_run):
    """Rebuild denormalised partition counters from the actual rows.

    ``Partition.total_files`` and ``unique_files`` are caches of the extracted
    file rows (kept for cheap display and the ``total_files > 0`` filter).  They
    are maintained incrementally and refreshed on hash rescans, but if they ever
    drift this command rebuilds every partition's counters from scratch.

    (``HashDatabase.file_count`` is a derived value with no stored column, so it
    never needs reconciling.)

    Safe to re-run at any time.

    Examples:

      docker compose exec web flask reconcile-counts
      docker compose exec web flask reconcile-counts --dry-run
    """
    pids = [row[0] for row in db.session.query(Partition.id).order_by(Partition.id).all()]
    total = len(pids)
    click.echo(f"Partitions to check: {total}")
    if total == 0:
        return

    if dry_run:
        # Report current vs recomputed without mutating anything.
        from sqlalchemy import and_, case, func
        from ..database import ExtractedFile
        unique_case = case(
            (and_(ExtractedFile.known_file_id.is_(None),
                  ExtractedFile.is_directory == False), 1),
            else_=0,
        )
        rows = dict(
            (pid, (t, u)) for pid, t, u in
            db.session.query(
                ExtractedFile.partition_id,
                func.count(ExtractedFile.id),
                func.coalesce(func.sum(unique_case), 0),
            ).group_by(ExtractedFile.partition_id).all()
        )
        drifted = 0
        for part in Partition.query.all():
            want_total, want_unique = rows.get(part.id, (0, 0))
            if (part.total_files or 0) != want_total or (part.unique_files or 0) != want_unique:
                drifted += 1
                click.echo(
                    f"  [dry-run] partition {part.id}: "
                    f"total_files {part.total_files} -> {want_total}, "
                    f"unique_files {part.unique_files} -> {want_unique}"
                )
        click.echo(f"Partitions with drifted counts: {drifted}")
        return

    done = 0
    for i in range(0, total, batch_size):
        # _refresh_partition_unique_counts recomputes both counters and commits.
        _refresh_partition_unique_counts(pids[i:i + batch_size])
        done += len(pids[i:i + batch_size])

    click.echo(f"Partitions reconciled: {done}")

# vim: ts=4 sw=4 et
