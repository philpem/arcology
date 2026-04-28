import click

from ..database import Artefact
from ..utils.hash_rescan import rescan_hashes_all, rescan_hashes_for_artefact


@click.command('rescan-hashes')
@click.option('--artefact', 'artefact_uuid', default=None,
              help='UUID of a single artefact to rescan (default: all artefacts)')
@click.option('--batch-size', default=500, show_default=True,
              help='Number of files to process per database commit')
def rescan_hashes(artefact_uuid, batch_size):
    """Re-link extracted files to active hash databases without re-analysing.

    Iterates ExtractedFile rows and updates is_known / known_file_id by
    matching each file's md5/sha1 against currently active hash databases.
    Also refreshes the unique_files counter on affected partitions.

    Run for a single artefact:

      docker compose exec web flask rescan-hashes --artefact <UUID>

    Run for the entire collection:

      docker compose exec web flask rescan-hashes
    """
    if artefact_uuid:
        artefact = Artefact.query.filter_by(uuid=artefact_uuid).first()
        if not artefact:
            click.echo(f"ERROR: artefact '{artefact_uuid}' not found.", err=True)
            raise SystemExit(1)
        click.echo(f"Rescanning hashes for artefact {artefact_uuid} ...")
        updated, total = rescan_hashes_for_artefact(artefact)
    else:
        click.echo("Rescanning hashes for all artefacts ...")
        updated, total = rescan_hashes_all(batch_size=batch_size)

    click.echo(f"Done. {updated} of {total} files updated.")

# vim: ts=4 sw=4 et
