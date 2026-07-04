import click
from ..database import Artefact
from ..extensions import db
from ..services.artefact_storage import compute_file_hashes_full, get_artefact_storage_key


@click.command('backfill-artefact-sha1')
@click.option('--batch-size', default=200, show_default=True,
              help='Number of rows to commit per batch')
@click.option('--artefact', 'artefact_uuid', default=None,
              help='Backfill a single artefact by UUID instead of all')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would be updated without making changes')
def backfill_artefact_sha1(batch_size, artefact_uuid, dry_run):
    """Compute SHA-1 for artefacts that don't have one yet.

    New uploads get their SHA-1 from the worker's CHECKSUM_COMPUTE analysis
    (alongside MD5/SHA-256), but artefacts uploaded before SHA-1 was tracked —
    and derived artefacts, whose hashes come from a blob record that carries no
    SHA-1 — have ``sha1 = NULL``.  This command streams each such artefact's
    stored bytes and fills in the SHA-1 without a full re-analysis.

    Safe to re-run: artefacts that already have a SHA-1 are skipped.  Files that
    can't be read (missing storage object) are skipped with a warning.

    Examples:

      docker compose exec web flask backfill-artefact-sha1
      docker compose exec web flask backfill-artefact-sha1 --dry-run
      docker compose exec web flask backfill-artefact-sha1 --artefact 1a2b3c4d...
    """
    query = Artefact.query.filter(
        Artefact.sha1.is_(None),
        Artefact.storage_path.isnot(None),
    )
    if artefact_uuid:
        query = query.filter(Artefact.uuid == artefact_uuid)
    query = query.order_by(Artefact.id)

    total = query.count()
    click.echo(f"Artefacts needing SHA-1 backfill: {total}")
    if total == 0:
        return

    updated = 0
    skipped = 0
    batch = 0
    for artefact in query:
        try:
            key = get_artefact_storage_key(artefact)
            _md5, sha1, _sha256 = compute_file_hashes_full(key, use_storage=True)
        except (OSError, ValueError) as exc:
            click.echo(f"  SKIP artefact {artefact.uuid[:8]}: {exc}")
            skipped += 1
            continue

        if dry_run:
            click.echo(f"  [dry-run] artefact {artefact.uuid[:8]} sha1={sha1}")
            updated += 1
            continue

        artefact.sha1 = sha1
        updated += 1
        batch += 1
        if batch >= batch_size:
            db.session.commit()
            batch = 0

    if not dry_run and batch:
        db.session.commit()

    verb = 'would be ' if dry_run else ''
    click.echo(f"Artefacts: {updated} SHA-1(s) {verb}computed, {skipped} skipped.")

# vim: ts=4 sw=4 et
