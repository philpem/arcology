import click
from flask import current_app
from sqlalchemy import func
from ..database import Artefact
from ..extensions import db


@click.command('dedup-artefacts')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show duplicates without deleting them')
def dedup_artefacts(dry_run):
    """Remove duplicate artefacts (same item + same SHA-256).

    Keeps the oldest artefact (lowest id) in each duplicate group and deletes
    the rest, including their stored files and queued analyses.
    """
    # Find (item_id, sha256) groups with more than one artefact.
    # Exclude artefacts without sha256 (not yet hashed) and derived artefacts.
    dupes = (
        db.session.query(Artefact.item_id, Artefact.sha256)
        .filter(Artefact.sha256.isnot(None))
        .filter(Artefact.derived_from_analysis_id.is_(None))
        .group_by(Artefact.item_id, Artefact.sha256)
        .having(func.count(Artefact.id) > 1)
        .all()
    )

    if not dupes:
        click.echo('No duplicate artefacts found.')
        return

    total_removed = 0
    for item_id, sha256 in dupes:
        artefacts = (
            Artefact.query
            .filter_by(item_id=item_id, sha256=sha256)
            .filter(Artefact.derived_from_analysis_id.is_(None))
            .order_by(Artefact.id)
            .all()
        )
        keep = artefacts[0]
        remove = artefacts[1:]

        for art in remove:
            click.echo(f'{"[DRY RUN] " if dry_run else ""}Removing duplicate: '
                       f'{art.uuid[:8]} "{art.label}" '
                       f'(keeping {keep.uuid[:8]} "{keep.label}") '
                       f'on item {art.item.name if art.item else art.item_id}')
            if not dry_run:
                # Delete stored file
                try:
                    from ..services.artefact_lifecycle import (
                        cleanup_artefact_outputs,
                        cleanup_artefact_outputs_s3,
                        delete_artefact_files,
                    )
                    delete_artefact_files(art)
                    from shared.storage import S3Storage
                    storage = current_app.storage
                    if isinstance(storage, S3Storage):
                        cleanup_artefact_outputs_s3(art, storage)
                    else:
                        cleanup_artefact_outputs(art, current_app.logger)
                except Exception as e:
                    click.echo(f'  Warning: file cleanup failed: {e}')

                db.session.delete(art)
                total_removed += 1

    if not dry_run:
        db.session.commit()
        click.echo(f'\nRemoved {total_removed} duplicate artefact(s).')
    else:
        click.echo(f'\n[DRY RUN] Would remove {len([a for _, _ in dupes for a in [1]])} duplicate group(s).')
        # Count actual duplicates
        total_would_remove = sum(
            Artefact.query
            .filter_by(item_id=item_id, sha256=sha256)
            .filter(Artefact.derived_from_analysis_id.is_(None))
            .count() - 1
            for item_id, sha256 in dupes
        )
        click.echo(f'[DRY RUN] Would remove {total_would_remove} duplicate artefact(s).')

# vim: ts=4 sw=4 et
