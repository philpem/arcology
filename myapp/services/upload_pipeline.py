"""
Arcology - Upload ingest pipeline

Single implementation of the post-storage upload steps shared by the web
upload form (``myapp/blueprints/artefacts.py``) and the REST API single and
chunked upload endpoints (``myapp/blueprints/api.py``).

The caller is responsible for getting the file into the storage backend
(under ``uploads/``) and computing its hashes; everything from the blob
assignment onwards happens here:

1. Artefact row creation.
2. Blob assignment — identical content already stored globally (same SHA-256
   + file_size) reuses the existing physical file; the newly uploaded
   duplicate is deleted immediately.
3. Slug generation and analysis queueing — all committed in a single
   transaction; on failure the stored file is deleted and the exception
   re-raised so no orphaned file or half-initialised artefact is left behind.
"""

from dataclasses import dataclass, field
from flask import current_app
from arcology_shared.enums import AnalysisType
from ..database import ANALYSIS_PRIORITY_NORMAL, Artefact, Item, StorageDirectory
from ..extensions import db
from ..services.artefact_types import queue_analyses_for_artefact
from ..utils.blobs import assign_blob as _assign_blob
from ..utils.slugs import ensure_unique_slug, generate_slug

# Analysis queueing modes for ingest_uploaded_artefact()
QUEUE_FULL = 'full'                    # CHECKSUM_COMPUTE + type-specific analyses
QUEUE_CHECKSUM_ONLY = 'checksum_only'  # CHECKSUM_COMPUTE only (web auto-analyse off)
QUEUE_NONE = 'none'                    # queue nothing (API auto_analyse=false)


@dataclass
class IngestOutcome:
    """Result of ingest_uploaded_artefact().

    Exactly one of ``artefact`` / ``duplicate`` is set.  ``queued_analyses``
    lists the AnalysisType members actually queued, including the implicit
    CHECKSUM_COMPUTE job.
    """
    artefact: Artefact | None = None
    duplicate: Artefact | None = None
    queued_analyses: list[AnalysisType] = field(default_factory=list)


def _delete_stored_file(storage_key: str) -> None:
    """Best-effort removal of an uploaded file during duplicate/error cleanup."""
    try:
        current_app.storage.delete(storage_key)
    except Exception:
        current_app.logger.warning(
            'Could not delete uploaded file %s during cleanup', storage_key,
            exc_info=True)


def ingest_uploaded_artefact(item: Item, *,
                             label: str,
                             artefact_type,
                             type_overridden: bool,
                             original_filename: str,
                             storage_name: str,
                             file_size: int,
                             md5: str | None,
                             sha256: str | None,
                             description: str | None = None,
                             mime_type: str | None = None,
                             owner_id: int | None = None,
                             is_private: bool = False,
                             hints: dict | None = None,
                             queue: str = QUEUE_FULL,
                             priority: int = ANALYSIS_PRIORITY_NORMAL) -> IngestOutcome:
    """Create an Artefact for an already-stored upload and queue its analyses.

    ``storage_name`` is the backend-relative name under ``uploads/`` returned
    by save_uploaded_file() (or built the same way by the chunked-upload
    assembler).  ``sha256`` may be None when hashing failed; the duplicate
    check is skipped in that case.

    The artefact row, its slug, and all queued Analysis rows are committed in
    a single transaction; on any failure the stored file is deleted and the
    exception re-raised (except for the duplicate-race IntegrityError, which
    resolves to the winning artefact).
    """
    storage_key = current_app.storage.storage_key('uploads', storage_name)

    artefact = Artefact(
        item_id=item.id,
        label=label,
        artefact_type=artefact_type,
        type_overridden=type_overridden,
        description=description,
        original_filename=original_filename,
        storage_path=storage_name,
        storage_directory=StorageDirectory.UPLOADS,
        file_size=file_size,
        mime_type=mime_type,
        md5=md5,
        sha256=sha256,
        owner_id=owner_id,
        is_private=is_private,
    )
    db.session.add(artefact)
    queued: list[AnalysisType] = []
    try:
        db.session.flush()  # assign artefact.id for the Analysis rows' FK
        blob, blob_created = _assign_blob(
            artefact, StorageDirectory.UPLOADS, storage_name, file_size, sha256, md5
        )
        if blob is not None and not blob_created and blob.storage_path != storage_name:
            # Identical content already stored globally: delete the new copy
            # and point the artefact's compat column at the canonical blob path.
            _delete_stored_file(storage_key)
            storage_key = None  # prevent double-delete in the except handler
            artefact.storage_path = blob.storage_path
        artefact.slug = ensure_unique_slug(
            generate_slug(label), Artefact, scope_filter={'item_id': item.id})
        if queue != QUEUE_NONE:
            # skip_duplicate_check: the artefact was created in this
            # transaction, so it cannot have pre-existing analyses.
            queued = queue_analyses_for_artefact(
                artefact, hints,
                checksum_only=(queue == QUEUE_CHECKSUM_ONLY),
                skip_duplicate_check=True,
                commit=False,
                priority=priority)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if storage_key is not None:
            _delete_stored_file(storage_key)
        raise
    return IngestOutcome(artefact=artefact, queued_analyses=queued)

# vim: ts=4 sw=4 et
