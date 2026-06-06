"""Helpers for assigning globally deduplicated artefact blobs."""

from sqlalchemy.exc import IntegrityError
from ..database import OutputBlob, StorageDirectory, UploadBlob
from ..extensions import db


def blob_model(storage_directory):
    if storage_directory == StorageDirectory.UPLOADS:
        return UploadBlob
    if storage_directory == StorageDirectory.OUTPUTS:
        return OutputBlob
    raise ValueError(f"Unsupported storage directory: {storage_directory!r}")


def get_or_create_blob(storage_directory, storage_path, file_size, sha256, md5=None):
    """Return ``(blob, created)`` for known content.

    Unknown size/hash values cannot be deduplicated and return ``(None, False)``.
    A size of zero is valid and is deliberately distinguished from ``None``.
    """
    if file_size is None or not sha256:
        return None, False

    model = blob_model(storage_directory)
    sha256 = sha256.lower()
    md5 = md5.lower() if md5 else None
    existing = model.query.filter_by(file_size=file_size, sha256=sha256).first()
    if existing:
        return existing, False

    blob = model(
        file_size=file_size,
        sha256=sha256,
        md5=md5,
        storage_path=storage_path,
    )
    try:
        with db.session.begin_nested():
            db.session.add(blob)
            db.session.flush()
        return blob, True
    except IntegrityError:
        existing = model.query.filter_by(file_size=file_size, sha256=sha256).first()
        if existing is None:
            raise
        return existing, False


def assign_blob(
    artefact,
    storage_directory,
    storage_path,
    file_size,
    sha256,
    md5=None,
    logical_storage_path=None,
):
    """Assign canonical blob storage to an artefact.

    Returns ``(blob, created)``. Compatibility storage/hash columns are kept in
    sync while callers and external API consumers migrate to blob relationships.
    """
    model = blob_model(storage_directory)
    current_blob = artefact_blob(artefact)
    normalised_sha256 = sha256.lower() if sha256 else None
    normalised_md5 = md5.lower() if md5 else None

    # Populate required compatibility fields before a lookup can trigger an
    # autoflush for an artefact that the caller has already added to the session.
    artefact.storage_directory = storage_directory
    artefact.file_size = file_size
    artefact.sha256 = normalised_sha256
    artefact.md5 = normalised_md5
    artefact.storage_path = (
        logical_storage_path if logical_storage_path is not None else storage_path
    )

    blob = None
    created = False
    if file_size is not None and normalised_sha256:
        blob = model.query.filter_by(
            file_size=file_size, sha256=normalised_sha256
        ).first()

    if (
        isinstance(current_blob, model)
        and current_blob.storage_path == storage_path
        and file_size is not None
        and normalised_sha256
        and (blob is None or blob is current_blob)
    ):
        # A checksum correction describes the same physical object. Updating
        # the shared blob also keeps every reference to those bytes consistent.
        current_blob.file_size = file_size
        current_blob.sha256 = normalised_sha256
        current_blob.md5 = normalised_md5
        for linked_artefact in current_blob.artefacts:
            linked_artefact.file_size = file_size
            linked_artefact.sha256 = normalised_sha256
            linked_artefact.md5 = normalised_md5
        blob = current_blob
    elif blob is None:
        blob, created = get_or_create_blob(
            storage_directory, storage_path, file_size, sha256, md5
        )

    if storage_directory == StorageDirectory.UPLOADS:
        artefact.upload_blob = blob
        artefact.output_blob = None
    else:
        artefact.output_blob = blob
        artefact.upload_blob = None
    return blob, created


def artefact_blob(artefact):
    upload_blob = getattr(artefact, "upload_blob", None)
    if isinstance(upload_blob, UploadBlob):
        return upload_blob
    output_blob = getattr(artefact, "output_blob", None)
    return output_blob if isinstance(output_blob, OutputBlob) else None


def artefact_blob_storage_path(artefact):
    """Return the physical blob path, falling back for legacy artefacts."""
    blob = artefact_blob(artefact)
    return blob.storage_path if blob is not None else artefact.storage_path
