import click
from ..database import Artefact, StorageDirectory
from ..extensions import db
from ..utils.blobs import assign_blob


@click.command('backfill-blobs')
@click.option('--batch-size', default=500, show_default=True,
              help='Number of rows to commit per batch')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would be updated without making changes')
def backfill_blobs(batch_size, dry_run):
    """Assign blob records to hashed artefacts that have no blob.

    After the global blob deduplication migration, artefacts whose SHA-256 was
    NULL at migration time (not yet computed by CHECKSUM_COMPUTE) have no blob
    record.  Once the worker fills in their SHA-256, this command creates the
    missing blob records without requiring a full re-analysis.

    Safe to re-run -- artefacts that already have a blob are skipped.

    Examples:

      docker compose exec web flask backfill-blobs
      docker compose exec web flask backfill-blobs --dry-run
      docker compose exec web flask backfill-blobs --batch-size 200
    """
    query = (
        Artefact.query
        .filter(
            Artefact.sha256.isnot(None),
            Artefact.file_size.isnot(None),
            db.or_(
                db.and_(
                    Artefact.storage_directory == StorageDirectory.UPLOADS,
                    Artefact.upload_blob_id.is_(None),
                ),
                db.and_(
                    Artefact.storage_directory == StorageDirectory.OUTPUTS,
                    Artefact.output_blob_id.is_(None),
                ),
            ),
        )
        .order_by(Artefact.id)
    )
    total = query.count()
    click.echo(f"Artefacts needing blob backfill: {total}")
    if total == 0:
        return

    count = 0
    batch = []
    for artefact in query:
        if dry_run:
            click.echo(
                f"  [dry-run] artefact {artefact.uuid[:8]} "
                f"({artefact.storage_directory.value}) sha256={artefact.sha256[:12]}..."
            )
        else:
            # For legacy artefacts (pre-blob-dedup), storage_path is the
            # physical file path in both UPLOADS and OUTPUTS. New-style output
            # artefacts use a logical storage_path but always have output_blob_id
            # set, so they are excluded by the query filter above.
            assign_blob(
                artefact,
                artefact.storage_directory,
                artefact.storage_path,
                artefact.file_size,
                artefact.sha256,
                artefact.md5,
            )
            batch.append(artefact)
            if len(batch) >= batch_size:
                db.session.commit()
                batch = []
        count += 1

    if not dry_run and batch:
        db.session.commit()

    click.echo(f"Artefacts: {count} blob(s) {'would be ' if dry_run else ''}assigned.")

# vim: ts=4 sw=4 et
