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
from ..database import (
    Artefact,
    ExtractedFile,
    Item,
    OutputBlob,
    Partition,
    StorageDirectory,
    UploadBlob,
    User,
)
from ..extensions import db
from .dedup import dedup_content_clause

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


def _clamp_percent(pct):
    """Clamp a percentage to [0, 100], passing ``None`` through unchanged."""
    if pct is None:
        return None
    return max(0.0, min(100.0, pct))


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
    # Intentionally system-wide: this is operational storage accounting for the
    # staff/admin `/storage` page, so it deliberately does NOT apply an item
    # *_visibility_clause (which would undercount and misreport dedup totals).
    # The route is gated to STAFF and admins, who are trusted operators.
    # dedup_content_clause excludes zero-length artefacts: every empty file
    # shares the canonical empty-file SHA-256, so they collapse into one large
    # group that wastes no physical bytes (file_size * (count - 1) == 0) yet
    # crowds out the genuine duplicates this list exists to surface.
    top_groups = db.session.execute(
        db.select(Artefact.file_size, Artefact.sha256, func.count(Artefact.id))
        .where(dedup_content_clause(Artefact))
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


def duplicate_group_instances(file_size: int, sha256: str) -> dict:
    """Every artefact and extracted file whose content is ``(file_size, sha256)``.

    Backs the staff/admin ``/storage`` drill-down so an operator can see *where*
    a duplicated content group lives — which items, artefacts, partitions and
    paths hold copies, and who owns them.  This distinguishes "the same file
    appears across several artefacts" from "one artefact was uploaded several
    times" (compare owners and original filenames).

    Intentionally system-wide: like ``deduplication_stats()`` this is
    operational accounting for a route gated to STAFF and admins, so it applies
    no item ``*_visibility_clause``.  Returns SQLAlchemy ``Row`` objects whose
    columns the template reads by name.
    """
    artefacts = db.session.execute(
        db.select(
            Artefact.uuid,
            Artefact.label,
            Artefact.original_filename,
            Artefact.storage_directory,
            Artefact.is_private,
            Artefact.created_at,
            Item.uuid.label('item_uuid'),
            Item.name.label('item_name'),
            User.username.label('owner'),
        )
        .join(Item, Artefact.item_id == Item.id)
        .outerjoin(User, Artefact.owner_id == User.id)
        .where(Artefact.file_size == file_size, Artefact.sha256 == sha256)
        .order_by(Item.name, Artefact.label)
    ).all()

    files = db.session.execute(
        db.select(
            ExtractedFile.uuid,
            ExtractedFile.path,
            ExtractedFile.filename,
            Partition.partition_index,
            Partition.label.label('partition_label'),
            Artefact.uuid.label('artefact_uuid'),
            Artefact.label.label('artefact_label'),
            Item.uuid.label('item_uuid'),
            Item.name.label('item_name'),
        )
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .where(
            ExtractedFile.file_size == file_size,
            ExtractedFile.sha256 == sha256,
            ExtractedFile.is_directory.is_(False),
        )
        .order_by(Item.name, Artefact.label, ExtractedFile.path)
    ).all()

    return {
        'file_size': file_size,
        'sha256': sha256,
        'artefacts': artefacts,
        'files': files,
        'outputs_dir': StorageDirectory.OUTPUTS,
    }


def storage_capacity(arcology_bytes: int | None = None) -> dict:
    """Capacity figures for display.

    ``used`` and ``arcology_bytes`` both report Arcology's own footprint
    (physical blob bytes) for every backend — there is no per-backend overload.
    Local backends additionally report the real filesystem ``total``/``free``/
    ``fs_used`` via ``statvfs`` (object stores report these as ``None``); for
    any backend an optional
    ``STORAGE_CAPACITY_BYTES`` config provides a quota ceiling (the only way to
    express a "total" for an unbounded object store).  ``percent_used`` is the
    fullness of the *displayed* capacity — disk fullness for local backends,
    quota fullness for object stores — clamped to ``[0, 100]``.

    Pass ``arcology_bytes`` to reuse a footprint already computed by
    ``deduplication_stats()`` and avoid re-summing the blob tables.
    """
    if arcology_bytes is None:
        _, upload_physical = _blob_totals(UploadBlob)
        _, output_physical = _blob_totals(OutputBlob)
        arcology_bytes = upload_physical + output_physical
    used = arcology_bytes

    disk = current_app.storage.disk_usage()
    quota = current_app.config.get('STORAGE_CAPACITY_BYTES')

    if disk is not None:
        total = disk['total']
        free = disk['free']
        fs_used = disk['used']
        percent = _clamp_percent((fs_used / total * 100.0) if total else None)
        return {
            'kind': 'local',
            'total': total,
            'free': free,
            'fs_used': fs_used,
            'used': used,
            'arcology_bytes': used,
            'percent_used': percent,
            'quota': quota,
        }

    # Object store: no filesystem free space.  Total is the configured quota.
    # Clamp free at zero and percent at 100 so an over-quota collection doesn't
    # render a negative free figure or a progress bar wider than its container.
    percent = _clamp_percent((used / quota * 100.0) if quota else None)
    return {
        'kind': 's3',
        'total': quota,
        'free': max(quota - used, 0) if quota is not None else None,
        'fs_used': None,  # No backing filesystem; keep the dict shape uniform.
        'used': used,
        'arcology_bytes': used,
        'percent_used': percent,
        'quota': quota,
    }


def navbar_storage_summary() -> dict | None:
    """Compact, briefly-cached capacity summary for the navbar chip.

    The headline figure is **free space** whenever a total is known — the disk
    size for local backends, or the configured ``STORAGE_CAPACITY_BYTES`` quota
    for object stores.  Only an object store with no quota configured (so no
    "total" to subtract from) falls back to reporting the stored footprint.

    Returns a dict with a compact ``label`` for the chip, a ``percent_used``
    (used to colour the chip when space runs low), and a list of
    ``(name, value)`` ``detail`` rows for the hover tooltip — or ``None`` if
    figures can't be produced.
    """
    now = time.monotonic()
    if _navbar_cache['value'] is not None and \
            (now - _navbar_cache['at']) < _NAVBAR_TTL_SECONDS:
        return _navbar_cache['value']

    try:
        cap = storage_capacity()
        total = cap['total']
        free = cap['free']
        used = cap['used']
        percent = cap['percent_used']

        if total is not None:
            # Free space is the headline; the tooltip carries the breakdown.
            label = f"{format_size(free)} free of {format_size(total)}"
            free_pct = round(100.0 - percent) if percent is not None else None
            total_name = 'Disk size' if cap['kind'] == 'local' else 'Quota'
            detail = [
                ('Collection', format_size(used)),
                ('Free', f"{format_size(free)} ({free_pct}%)"
                         if free_pct is not None else format_size(free)),
                (total_name, format_size(total)),
            ]
        else:
            # Object store with no quota: no total to report free against.
            label = f"{format_size(used)} stored"
            detail = [('Collection', format_size(used))]

        summary = {
            'kind': cap['kind'],
            'label': label,
            'percent_used': percent,
            'detail': detail,
        }
    except Exception:  # pragma: no cover - never break page render over a chip
        summary = None

    _navbar_cache['at'] = now
    _navbar_cache['value'] = summary
    return summary


# vim: ts=4 sw=4 et
