"""
Storage capacity and deduplication statistics.

Single source of truth for the figures shown on the staff/admin ``/storage``
page and the navbar capacity chip.  All deduplication numbers are cheap DB
aggregates over the blob tables (``UploadBlob`` / ``OutputBlob``); no storage
backend enumeration is performed.

Physical content is stored once per ``(file_size, sha256)`` blob, while many
``Artefact`` rows may reference one blob.  "Logical" bytes are what the
collection would occupy *without* deduplication (each referencing artefact
counted); "physical" bytes are what is actually stored (each blob counted once).
"""

import time
from flask import current_app
from sqlalchemy import func
from ..database import Artefact, OutputBlob, UploadBlob
from ..extensions import db

# Most-duplicated content groups shown on the stats page.
TOP_DUPLICATE_GROUPS = 20

# Navbar summary cache — a statvfs syscall plus two SUM() queries is cheap, but
# the navbar renders on every page, so memoise the result briefly.
_NAVBAR_TTL_SECONDS = 60
_navbar_cache: dict = {'at': 0.0, 'value': None}


def format_size(num) -> str:
    """Human-readable byte count (e.g. ``1.5 GB``).  ``None`` renders as ``-``."""
    if num is None:
        return '-'
    n = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f"{int(n)} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def _blob_totals(model):
    """Return (count, bytes) of stored blobs for a blob model (physical)."""
    count, total = db.session.execute(
        db.select(func.count(model.id), func.coalesce(func.sum(model.file_size), 0))
    ).one()
    return int(count), int(total)


def _logical_bytes(blob_model, fk_column):
    """Sum of blob sizes counted once per referencing artefact (logical)."""
    return int(db.session.scalar(
        db.select(func.coalesce(func.sum(blob_model.file_size), 0))
        .select_from(Artefact)
        .join(blob_model, fk_column == blob_model.id)
    ) or 0)


def _shared_blob_count(fk_column):
    """Number of blobs referenced by more than one artefact."""
    grouped = (
        db.select(fk_column)
        .where(fk_column.isnot(None))
        .group_by(fk_column)
        .having(func.count(Artefact.id) > 1)
        .subquery()
    )
    return int(db.session.scalar(db.select(func.count()).select_from(grouped)) or 0)


def deduplication_stats() -> dict:
    """Compute deduplication statistics across upload and output blobs."""
    upload_count, upload_physical = _blob_totals(UploadBlob)
    output_count, output_physical = _blob_totals(OutputBlob)
    physical = upload_physical + output_physical

    upload_logical = _logical_bytes(UploadBlob, Artefact.upload_blob_id)
    output_logical = _logical_bytes(OutputBlob, Artefact.output_blob_id)
    logical = upload_logical + output_logical

    saved = logical - physical
    ratio = (logical / physical) if physical else None

    shared = _shared_blob_count(Artefact.upload_blob_id) + \
        _shared_blob_count(Artefact.output_blob_id)

    # Most-duplicated logical content (same query as `flask dedup-artefacts`).
    top_groups = db.session.execute(
        db.select(Artefact.file_size, Artefact.sha256, func.count(Artefact.id))
        .where(Artefact.file_size.isnot(None), Artefact.sha256.isnot(None))
        .group_by(Artefact.file_size, Artefact.sha256)
        .having(func.count(Artefact.id) > 1)
        .order_by(func.count(Artefact.id).desc())
        .limit(TOP_DUPLICATE_GROUPS)
    ).all()

    return {
        'blob_count': upload_count + output_count,
        'upload_blob_count': upload_count,
        'output_blob_count': output_count,
        'physical_bytes': physical,
        'upload_physical_bytes': upload_physical,
        'output_physical_bytes': output_physical,
        'logical_bytes': logical,
        'saved_bytes': saved,
        'dedup_ratio': ratio,
        'shared_blob_count': shared,
        'top_groups': [
            {'file_size': fs, 'sha256': sha, 'count': int(c)}
            for fs, sha, c in top_groups
        ],
    }


def storage_capacity() -> dict:
    """Capacity figures for display.

    ``used`` is Arcology's own footprint (physical blob bytes).  Local backends
    add real filesystem ``total``/``free`` via ``statvfs``; for any backend an
    optional ``STORAGE_CAPACITY_BYTES`` config provides a quota ceiling (the
    only way to express a "total" for an unbounded object store).
    """
    _, upload_physical = _blob_totals(UploadBlob)
    _, output_physical = _blob_totals(OutputBlob)
    used = upload_physical + output_physical

    disk = current_app.storage.disk_usage()
    quota = current_app.config.get('STORAGE_CAPACITY_BYTES')

    if disk is not None:
        total = disk['total']
        free = disk['free']
        # `used` here is whole-filesystem usage, the honest free-space figure.
        fs_used = disk['used']
        percent = (fs_used / total * 100.0) if total else None
        return {
            'kind': 'local',
            'total': total,
            'free': free,
            'used': fs_used,
            'arcology_bytes': used,
            'percent_used': percent,
            'quota': quota,
        }

    # Object store: no filesystem free space.  Total is the configured quota.
    percent = (used / quota * 100.0) if quota else None
    return {
        'kind': 's3',
        'total': quota,
        'free': (quota - used) if quota is not None else None,
        'used': used,
        'arcology_bytes': used,
        'percent_used': percent,
        'quota': quota,
    }


def navbar_storage_summary() -> dict | None:
    """Compact, briefly-cached capacity summary for the navbar chip.

    Local → free/total of the uploads volume.  Object store → stored bytes plus
    quota when configured.  Returns ``None`` if figures can't be produced.
    """
    now = time.monotonic()
    if _navbar_cache['value'] is not None and \
            (now - _navbar_cache['at']) < _NAVBAR_TTL_SECONDS:
        return _navbar_cache['value']

    try:
        cap = storage_capacity()
        if cap['kind'] == 'local':
            summary = {
                'kind': 'local',
                'label': f"{format_size(cap['free'])} free of {format_size(cap['total'])}",
                'percent_used': cap['percent_used'],
            }
        else:
            if cap['quota']:
                label = f"{format_size(cap['used'])} of {format_size(cap['quota'])} used"
            else:
                label = f"{format_size(cap['used'])} stored"
            summary = {
                'kind': 's3',
                'label': label,
                'percent_used': cap['percent_used'],
            }
    except Exception:  # pragma: no cover - never break page render over a chip
        summary = None

    _navbar_cache['at'] = now
    _navbar_cache['value'] = summary
    return summary


# vim: ts=4 sw=4 et
