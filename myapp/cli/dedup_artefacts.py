import click
from flask import current_app
from sqlalchemy import func
from ..database import Artefact, OutputBlob, UploadBlob
from ..extensions import db


def _format_size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} {unit}"
        n /= 1024


@click.command("dedup-artefacts")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Delete non-canonical legacy objects from the configured storage backend.",
)
def dedup_artefacts(apply):
    """Report repeated content and optionally prune legacy duplicate storage objects.

    Physical content is deduplicated globally by UploadBlob and OutputBlob.
    Identical Artefact rows may intentionally have different owners, privacy,
    labels, and lineage; this command never deletes Artefact rows.

    Non-canonical objects are storage paths on artefacts that do not match any
    blob record -- typically left over from uploads that predated the blob
    deduplication migration.  Removing them reclaims disk/object-store space.
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
    # Only upload artefacts can have legacy duplicate physical paths.  Output
    # artefacts use a logical storage_path (lineage key) that is intentionally
    # different from the physical blob path, so they must never be flagged.
    candidates = set()
    rows = db.session.query(
        Artefact.storage_path,
    ).filter(
        Artefact.upload_blob_id.isnot(None)
    )
    for (storage_path,) in rows:
        if storage_path and ("uploads", storage_path) not in canonical:
            candidates.add(("uploads", storage_path))

    if not candidates:
        click.echo("No non-canonical legacy objects found.")
        return

    # Compute estimated reclaimable size from artefact rows (blob.file_size is
    # the authoritative size; fall back to artefact.file_size for legacy rows).
    candidate_paths = {p for _, p in candidates}
    size_rows = db.session.query(
        Artefact.storage_path, Artefact.file_size
    ).filter(Artefact.storage_path.in_(candidate_paths)).all()
    total_bytes = sum(fs for _, fs in size_rows if fs is not None)
    click.echo(
        f"{len(candidates)} non-canonical object(s) found, "
        f"~{_format_size(total_bytes)} reclaimable."
    )
    click.echo("(Artefact records are not modified -- only orphaned storage objects are removed.)")

    verb = "Deleting" if apply else "Would delete"
    for domain, storage_path in sorted(candidates):
        key = current_app.storage.storage_key(domain, storage_path)
        click.echo(f"{verb}: {key}")
        if apply:
            current_app.storage.delete(key)

    if not apply:
        click.echo("Run again with --apply to remove these objects.")


# vim: ts=4 sw=4 et
