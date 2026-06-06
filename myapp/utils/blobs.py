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
    blob, created = get_or_create_blob(
        storage_directory, storage_path, file_size, sha256, md5
    )

    artefact.storage_directory = storage_directory
    artefact.file_size = file_size
    artefact.sha256 = sha256.lower() if sha256 else None
    artefact.md5 = md5.lower() if md5 else None
    artefact.storage_path = logical_storage_path or storage_path

    if storage_directory == StorageDirectory.UPLOADS:
        artefact.upload_blob = blob
        artefact.output_blob = None
    else:
        artefact.output_blob = blob
        artefact.upload_blob = None
    return blob, created


def artefact_blob(artefact):
    return artefact.upload_blob or artefact.output_blob
