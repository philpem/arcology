"""Deduplicate and re-do content-addressed media transcode outputs.

New transcodes are content-addressed at write time: the worker stores the
MP4/poster under ``media/{source_sha256}/{tool_version}/`` and the web app links
a shared, refcounted :class:`OutputBlob` (see ``search_index._link_transcode_blobs``).
Two artefacts holding the byte-identical source therefore share one stored
output automatically.

Two operator tasks are *not* covered by that write-time path and live here:

* **Backfill** (:func:`dedup_transcode_outputs`) — collapse *legacy* duplicate
  transcodes that accumulated before content-addressing existed, **without**
  re-running the (expensive) transcode.  The source file's SHA-256 is already
  recorded (on the ``ExtractedFile`` or, for a direct upload, the ``Artefact``),
  so the canonical path is known without touching the transcoder.  The one
  surviving copy per source is hashed to record the true output hash on the
  blob; identical-source duplicates link to it and their redundant copies are
  removed.

* **Redo** (:func:`invalidate_transcodes`) — when a transcode was produced
  incorrectly, a plain reanalyse is a cache *hit* (keyed on the unchanged
  ``(source_sha256, tool_version)``) and returns the bad bytes.  Invalidation
  deletes the cached blob(s) + files and clears every referencing row so the
  next analysis re-encodes from scratch.  A bad transcode of a given source is
  bad for *every* artefact sharing that source, so invalidation is scoped by
  source hash.
"""

import posixpath
from flask import current_app
from arcology_shared.enums import AnalysisType
from arcology_shared.transcode_paths import (
    MEDIA_TRANSCODE_TOOL_VERSION,
    transcode_movie_name,
    transcode_output_subdir,
    transcode_poster_name,
)
from ..database import (
    Artefact,
    ExtractedFile,
    MediaFile,
    OutputBlob,
    Partition,
    ReplayMovie,
    StorageDirectory,
)
from ..extensions import db
from ..utils.blobs import get_or_create_blob
from .artefact_storage import compute_file_hashes

# The two row types that own a transcoded output, each mapped to the analysis
# that (re)produces it.  REPLAY_PROCESS both parses and transcodes ARMovie files
# (re-running it re-encodes when the cache is cold).  Both row types carry the
# same columns (mp4_output_path / poster_path and the mp4_output_blob_id /
# poster_blob_id dedup anchors), so they are processed uniformly.
_TRANSCODE_TYPES = {
    ReplayMovie: AnalysisType.REPLAY_PROCESS,
    MediaFile: AnalysisType.MEDIA_TRANSCODE,
}
_TRANSCODE_MODELS = tuple(_TRANSCODE_TYPES)


def _ext_of(path):
    """Output extension (no dot, lowercased) of a stored path, or ``None``."""
    if not path:
        return None
    return posixpath.splitext(path)[1].lstrip('.').lower() or None


def source_sha256_for(row):
    """SHA-256 of the source media a transcode row was produced from.

    Already recorded by earlier analysis — never recomputed here:

    * in-extraction movie (``file_path`` set) -> the matching ``ExtractedFile``;
    * direct media upload (``file_path`` NULL) -> the owning ``Artefact``.

    Returns ``None`` when the source hash cannot be resolved unambiguously, so
    the caller skips (and reports) the row rather than guessing.
    """
    if row.file_path:
        shas = db.session.scalars(
            db.select(ExtractedFile.sha256)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .where(
                Partition.artefact_id == row.artefact_id,
                ExtractedFile.path == row.file_path,
                ExtractedFile.sha256.isnot(None),
            )
            .distinct()
        ).all()
        # Exactly one distinct hash is unambiguous; 0 (not hashed / path drift)
        # or >1 (same path in sibling partitions) are not safe to dedup blindly.
        # Lowercase to match the worker's content-addressed path (compute_file_hash
        # emits lowercase hex) and redo's --source-hash normalisation.
        return shas[0].lower() if len(shas) == 1 and shas[0] else None

    artefact = db.session.get(Artefact, row.artefact_id)
    return artefact.sha256.lower() if artefact and artefact.sha256 else None


def _canonical_path(source_sha, leaf):
    return f'{transcode_output_subdir(source_sha, MEDIA_TRANSCODE_TOOL_VERSION)}/{leaf}'


def _hash_output(storage_path):
    """Return ``(sha256, md5, file_size)`` of an output object, streamed (no OOM).

    Returns ``(None, None, None)`` when the object is missing.
    """
    storage = current_app.storage
    key = storage.storage_key('outputs', storage_path)
    if not storage.exists(key):
        return None, None, None
    md5, sha256, size = compute_file_hashes(key, use_storage=True, with_size=True)
    return sha256, md5, size


def _relocate(src_path, dst_path):
    """Move an output object to its canonical ``dst_path`` (no-op if already so).

    Delegates to the backend's native move (filesystem rename / S3 CopyObject),
    so large outputs are not streamed through the web process.
    """
    if src_path == dst_path:
        return
    storage = current_app.storage
    storage.move(
        storage.storage_key('outputs', src_path),
        storage.storage_key('outputs', dst_path))


class DedupStats:
    """Counters reported by the dedup backfill."""

    def __init__(self):
        self.linked = 0           # rows newly linked to a shared output blob
        self.blobs_created = 0     # canonical outputs registered (hashed once)
        self.files_reclaimed = 0   # redundant duplicate objects deleted
        self.skipped = 0           # rows whose source/output could not resolve


def _link_output(row, fk_attr, path_attr, canonical, stats, dry_run):
    """Link one output column (mp4 or poster) on ``row`` to its canonical blob.

    Returns ``True`` if the column was (or would be) linked.
    """
    current_path = getattr(row, path_attr)
    if not current_path:
        return False
    storage = current_app.storage

    blob = OutputBlob.query.filter_by(storage_path=canonical).first()
    if blob is not None:
        # Canonical output already registered (by a prior new-scheme transcode
        # or an earlier same-source row in this run): link and drop the dup.
        if current_path != blob.storage_path and storage.exists(
                storage.storage_key('outputs', current_path)):
            if not dry_run:
                storage.delete(storage.storage_key('outputs', current_path))
            stats.files_reclaimed += 1
        if not dry_run:
            setattr(row, fk_attr, blob.id)
            setattr(row, path_attr, blob.storage_path)
        return True

    # First copy of this source's output. In a dry run, only confirm the file
    # exists (cheap) rather than hashing it.
    if dry_run:
        if not storage.exists(storage.storage_key('outputs', current_path)):
            stats.skipped += 1
            return False
        stats.blobs_created += 1
        return True

    sha256, md5, size = _hash_output(current_path)
    if sha256 is None:
        stats.skipped += 1
        return False
    # Register the blob FIRST, then move the file — only once we know a new
    # canonical output is genuinely needed.  get_or_create_blob keys on
    # (file_size, sha256), so a byte-identical output produced for a DIFFERENT
    # source (e.g. two clips sharing a title-card poster sprite) returns that
    # source's existing blob, whose storage_path != canonical.  In that case the
    # bytes already live at blob.storage_path: drop this duplicate and link the
    # existing path, mirroring search_index._link_transcode_blobs so the row,
    # owner-resolution and refcount GC all agree on one canonical path.
    blob, created = get_or_create_blob(
        StorageDirectory.OUTPUTS, canonical, size, sha256, md5)
    if blob is None:
        stats.skipped += 1
        return False
    if created:
        _relocate(current_path, canonical)
        stats.blobs_created += 1
    elif current_path != blob.storage_path and storage.exists(
            storage.storage_key('outputs', current_path)):
        storage.delete(storage.storage_key('outputs', current_path))
        stats.files_reclaimed += 1
    setattr(row, fk_attr, blob.id)
    setattr(row, path_attr, blob.storage_path)
    return True


def dedup_transcode_outputs(dry_run=False, batch_size=200):
    """Collapse legacy duplicate transcode outputs onto shared blobs.

    Idempotent and safe to re-run: rows already linked to a blob are skipped.
    Returns a :class:`DedupStats`.
    """
    stats = DedupStats()
    pending = 0
    for model in _TRANSCODE_MODELS:
        rows = db.session.scalars(
            db.select(model).where(
                model.mp4_output_blob_id.is_(None),
                model.mp4_output_path.isnot(None),
            ).order_by(model.id)
        ).all()
        for row in rows:
            source_sha = source_sha256_for(row)
            if not source_sha:
                stats.skipped += 1
                continue

            mp4_ext = _ext_of(row.mp4_output_path) or 'mp4'
            linked = _link_output(
                row, 'mp4_output_blob_id', 'mp4_output_path',
                _canonical_path(source_sha, transcode_movie_name(mp4_ext)),
                stats, dry_run)

            if getattr(row, 'poster_path', None):
                poster_ext = _ext_of(row.poster_path) or 'png'
                _link_output(
                    row, 'poster_blob_id', 'poster_path',
                    _canonical_path(
                        source_sha, transcode_poster_name(f'.{poster_ext}')),
                    stats, dry_run)

            if linked:
                stats.linked += 1
            if not dry_run:
                pending += 1
                if pending >= batch_size:
                    db.session.commit()
                    pending = 0
    if not dry_run and pending:
        db.session.commit()
    return stats


def invalidate_transcodes(source_hashes, dry_run=False):
    """Delete cached transcode outputs for the given source hashes.

    Removes the ``OutputBlob`` rows + storage objects under each source's
    content-addressed dir and clears the path/FK columns on every referencing
    row, so a subsequent analysis re-encodes from scratch.  Returns
    ``{'blobs': n, 'rows': n, 'objects': n}``.
    """
    counts = {'blobs': 0, 'rows': 0, 'objects': 0}
    storage = current_app.storage
    for source_sha in source_hashes:
        subdir = transcode_output_subdir(source_sha, MEDIA_TRANSCODE_TOOL_VERSION)
        prefix = subdir + '/'

        blob_ids = db.session.scalars(
            db.select(OutputBlob.id).where(
                OutputBlob.storage_path.like(prefix + '%'))
        ).all()
        counts['blobs'] += len(blob_ids)

        for model in _TRANSCODE_MODELS:
            rows = db.session.scalars(
                db.select(model).where(model.mp4_output_path.like(prefix + '%'))
            ).all()
            counts['rows'] += len(rows)
            if not dry_run:
                for row in rows:
                    row.mp4_output_path = None
                    row.poster_path = None
                    row.mp4_output_blob_id = None
                    row.poster_blob_id = None

        if dry_run:
            # Cheap, non-destructive estimate of what would be removed.
            counts['objects'] += len(blob_ids)
            continue

        if blob_ids:
            db.session.query(OutputBlob).filter(
                OutputBlob.id.in_(blob_ids)).delete(synchronize_session=False)
        counts['objects'] += storage.delete_prefix(
            storage.storage_key('outputs', subdir))
        db.session.commit()
    return counts


def source_hashes_for_artefact(artefact_id):
    """Distinct source hashes of every transcode owned by an artefact."""
    hashes = set()
    for model in _TRANSCODE_MODELS:
        rows = db.session.scalars(
            db.select(model).where(model.artefact_id == artefact_id)
        ).all()
        for row in rows:
            sha = source_sha256_for(row)
            if sha:
                hashes.add(sha)
    return hashes


def requeue_targets(source_hashes):
    """``{(artefact_id, AnalysisType)}`` to re-run after invalidating a source.

    Resolved from the rows that currently reference each source's content-
    addressed dir, so it MUST be called *before* :func:`invalidate_transcodes`
    (which clears those references).  A source shared by several artefacts yields
    one target per owning artefact, so every consumer is re-encoded.
    """
    targets = set()
    for source_sha in source_hashes:
        prefix = transcode_output_subdir(
            source_sha, MEDIA_TRANSCODE_TOOL_VERSION) + '/'
        for model, analysis_type in _TRANSCODE_TYPES.items():
            ids = db.session.scalars(
                db.select(model.artefact_id)
                .where(model.mp4_output_path.like(prefix + '%'))
                .distinct()
            ).all()
            for artefact_id in ids:
                targets.add((artefact_id, analysis_type))
    return targets

# vim: ts=4 sw=4 et
