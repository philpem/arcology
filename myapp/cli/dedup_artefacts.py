import click
from flask import current_app
from sqlalchemy import func

from ..database import Artefact, OutputBlob, StorageDirectory, UploadBlob
from ..extensions import db


@click.command("dedup-artefacts")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Delete non-canonical legacy objects from the configured storage backend.",
)
def dedup_artefacts(apply):
    """Report repeated content and optionally prune legacy duplicate objects.

    Physical content is deduplicated globally by UploadBlob and OutputBlob.
    Identical Artefact rows may intentionally have different owners, privacy,
    labels, and lineage; this command never deletes Artefact rows.
    """
    groups = (
        db.session.query(Artefact.file_size, Artefact.sha256, func.count(Artefact.id))
        .filter(Artefact.file_size.isnot(None), Artefact.sha256.isnot(None))
        .group_by(Artefact.file_size, Artefact.sha256)
        .having(func.count(Artefact.id) > 1)
        .order_by(func.count(Artefact.id).desc())
        .all()
    )

    if groups:
        click.echo(
            "Logical artefacts are retained; physical bytes are deduplicated by blob."
        )
        for file_size, sha256, count in groups:
            click.echo(f"{count:6d}  {file_size:12d}  {sha256}")
    else:
        click.echo("No repeated artefact content found.")

    canonical = {
        ("uploads", path) for (path,) in db.session.query(UploadBlob.storage_path)
    } | {
        ("outputs", path) for (path,) in db.session.query(OutputBlob.storage_path)
    }
    candidates = set()
    rows = db.session.query(
        Artefact.storage_directory,
        Artefact.storage_path,
        Artefact.upload_blob_id,
        Artefact.output_blob_id,
    ).filter(
        (Artefact.upload_blob_id.isnot(None))
        | (Artefact.output_blob_id.isnot(None))
    )
    for directory, storage_path, upload_blob_id, output_blob_id in rows:
        domain = (
            "outputs" if directory == StorageDirectory.OUTPUTS else "uploads"
        )
        if storage_path and (domain, storage_path) not in canonical:
            candidates.add((domain, storage_path))

    if not candidates:
        click.echo("No non-canonical legacy objects found.")
        return

    verb = "Deleting" if apply else "Would delete"
    for domain, storage_path in sorted(candidates):
        key = current_app.storage.storage_key(domain, storage_path)
        click.echo(f"{verb}: {key}")
        if apply:
            current_app.storage.delete(key)

    if not apply:
        click.echo("Run again with --apply to remove these objects.")


# vim: ts=4 sw=4 et
