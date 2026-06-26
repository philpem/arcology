"""
Shared predicate for "meaningful duplicate content".

Zero-length files all share the canonical empty-file SHA-256
(``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``), so
grouping them as duplicates collapses every empty file in the catalogue into
one group that wastes no physical bytes yet swamps the genuine duplicates.
Both the storage dedup accounting (``/storage``, ``flask dedup-artefacts``) and
the per-file duplicate badge / duplicates list exclude empty content, so the
rule lives here in one place rather than being re-spelled at each call site.
"""

from sqlalchemy import and_


def dedup_content_clause(model):
    """SQLAlchemy filter selecting rows of ``model`` whose ``(file_size,
    sha256)`` is a meaningful content key for duplicate accounting.

    ``model`` must expose ``file_size`` and ``sha256`` columns (e.g. ``Artefact``
    or ``ExtractedFile``).  ``file_size > 0`` also excludes NULL sizes — ``NULL >
    0`` is false in SQL — so this subsumes an ``IS NOT NULL`` guard on the size.
    """
    return and_(model.file_size > 0, model.sha256.isnot(None))


def has_dedup_content(file_size, sha256) -> bool:
    """Python mirror of :func:`dedup_content_clause` for already-loaded rows:
    a truthy size (excludes both ``0`` and ``None``) and a present hash."""
    return bool(file_size) and bool(sha256)


# vim: ts=4 sw=4 et
