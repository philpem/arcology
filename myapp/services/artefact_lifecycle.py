"""Arcology - Artefact lifecycle service

Deletion cascades, reanalysis reset, analysis-output cleanup, item bulk
delete, artefact move, and the processing-tree builder.  Shared by the web
blueprints, the REST API, and the Flask CLI commands (reanalyse,
dedup-artefacts) — previously these reached into the artefacts blueprint's
private helpers.

Moved verbatim from myapp/blueprints/artefacts.py.
"""

import json
import os
import shutil
from flask import current_app
from sqlalchemy import or_, select
from arcology_shared.hints import HintKey
from ..database import (
    ANALYSIS_PRIORITY_NORMAL,
    Analysis,
    AnalysisStatus,
    AnalysisType,
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    ArtefactRestriction,
    ExternalReference,
    ExtractedFile,
    ExtractedFileRestriction,
    Item,
    MediaFile,
    OutputBlob,
    Partition,
    RecognisedProduct,
    ReplayMovie,
    RiscosModule,
    StorageDirectory,
    UploadBlob,
    artefact_tags,
    item_tags,
)
from ..extensions import db
from ..utils.blobs import artefact_blob
from ..utils.slugs import ensure_unique_slug
from ..visibility import can_change_owner, can_contribute_to_item
from .artefact_storage import (
    get_artefact_storage_key,
    get_output_folder,
    map_output_path_to_local_root,
)


def get_all_derived_artefact_ids(artefact: Artefact) -> list[int]:
    """Collect all derived artefact IDs using a single recursive CTE query.

    Replaces the previous recursive ORM walk which triggered N+1 queries
    (one per level of the derivation tree).
    """
    base = select(Artefact.id).where(Artefact.parent_artefact_id == artefact.id)
    cte = base.cte(name='derived', recursive=True)
    recursive = select(Artefact.id).where(Artefact.parent_artefact_id == cte.c.id)
    cte = cte.union_all(recursive)
    rows = db.session.execute(select(cte.c.id)).all()
    return [r[0] for r in rows]


def collect_all_analyses(artefact: Artefact) -> list:
    """Collect all analyses for an artefact and its derived artefacts.

    Uses the CTE-based get_all_derived_artefact_ids to avoid N+1 queries,
    then fetches all analyses in a single query.
    """
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    return Analysis.query.filter(Analysis.artefact_id.in_(all_ids)).order_by(Analysis.id.desc()).all()


# Default keyset batch size for the task-runner's batched extracted-file
# deletion.  Small enough to commit + heartbeat well within the stale timeout,
# large enough that the per-statement overhead stays negligible.
EXTRACTED_FILE_DELETE_BATCH = 5000


def _noop_heartbeat(**_kwargs):
    pass


def _noop_check_cancelled():
    pass


def _delete_extracted_files(partition_ids, *, batch_size=None,
                            heartbeat=_noop_heartbeat,
                            check_cancelled=_noop_check_cancelled,
                            commit=False) -> int:
    """Delete ExtractedFile rows (and their restrictions) for *partition_ids*.

    When *batch_size* is falsy this is the historical single-statement delete
    (one transaction, no intermediate commits) used by the synchronous path.

    When *batch_size* is given (task-runner job path) the extracted files are
    deleted in keyset batches on ``ExtractedFile.id`` so the work is bounded per
    statement, committed between batches (releasing locks), and heartbeated.
    The self-referential ``parent_file_id`` (nested-archive children) is nulled
    out first so per-batch deletes can't violate that FK on PostgreSQL when a
    parent is removed in an earlier batch than its child.

    Returns the number of extracted files deleted (0 in the unbatched path,
    which does not count).
    """
    if not partition_ids:
        return 0

    if not batch_size:
        ef_subq = db.session.query(ExtractedFile.id).filter(
            ExtractedFile.partition_id.in_(partition_ids)).subquery()
        ExtractedFileRestriction.query.filter(
            ExtractedFileRestriction.extracted_file_id.in_(
                db.session.query(ef_subq.c.id)
            )).delete(synchronize_session=False)
        ExtractedFile.query.filter(
            ExtractedFile.partition_id.in_(partition_ids)
        ).delete(synchronize_session=False)
        return 0

    # Batched path: break the self-FK first so any delete order is safe.
    ExtractedFile.query.filter(
        ExtractedFile.partition_id.in_(partition_ids),
        ExtractedFile.parent_file_id.isnot(None),
    ).update({ExtractedFile.parent_file_id: None}, synchronize_session=False)
    if commit:
        db.session.commit()

    deleted = 0
    cursor = 0
    while True:
        check_cancelled()
        ids = [r[0] for r in db.session.query(ExtractedFile.id).filter(
            ExtractedFile.partition_id.in_(partition_ids),
            ExtractedFile.id > cursor,
        ).order_by(ExtractedFile.id).limit(batch_size).all()]
        if not ids:
            break
        ExtractedFileRestriction.query.filter(
            ExtractedFileRestriction.extracted_file_id.in_(ids)
        ).delete(synchronize_session=False)
        ExtractedFile.query.filter(
            ExtractedFile.id.in_(ids)
        ).delete(synchronize_session=False)
        cursor = ids[-1]
        deleted += len(ids)
        if commit:
            db.session.commit()
        heartbeat(current=deleted, label='Deleting extracted files…')
    return deleted


def bulk_delete_artefact_dependents(artefact_ids: list[int], *,
                                    batch_size=None,
                                    heartbeat=_noop_heartbeat,
                                    check_cancelled=_noop_check_cancelled,
                                    commit=False) -> int:
    """Bulk-delete all referencing rows across every FK table for the given artefact IDs.

    Deletes analyses, replay movies, extracted file restrictions, recognised
    products, extracted files, partitions, protection/mastering/module records,
    artefact restrictions, and tag associations.  Does NOT delete the
    artefacts themselves — call ``bulk_delete_artefacts`` for that.

    When *batch_size* is given the (potentially huge) extracted-file deletion is
    keyset-batched with ``commit`` between batches and ``heartbeat`` /
    ``check_cancelled`` callbacks, so the task runner can offload a large
    deletion without holding one giant transaction.  Returns the number of
    extracted files deleted (0 in the unbatched/synchronous path).
    """
    Analysis.query.filter(Analysis.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    # ReplayMovie has a plain (non-cascading) FK to artefacts, so it must be
    # deleted explicitly before the artefact rows go (the old ORM-cascade path
    # handled this via relationship cascade).
    ReplayMovie.query.filter(ReplayMovie.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    partition_ids = [r[0] for r in db.session.query(Partition.id).filter(
        Partition.artefact_id.in_(artefact_ids)).all()]
    if partition_ids:
        RecognisedProduct.query.filter(
            RecognisedProduct.partition_id.in_(partition_ids)
        ).delete(synchronize_session=False)
    deleted = _delete_extracted_files(
        partition_ids, batch_size=batch_size, heartbeat=heartbeat,
        check_cancelled=check_cancelled, commit=commit)
    Partition.query.filter(Partition.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactProtection.query.filter(ArtefactProtection.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactMastering.query.filter(ArtefactMastering.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    RiscosModule.query.filter(RiscosModule.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactRestriction.query.filter(ArtefactRestriction.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    db.session.execute(artefact_tags.delete().where(artefact_tags.c.artefact_id.in_(artefact_ids)))
    return deleted


def bulk_delete_artefacts(artefact_ids: list[int]):
    """Delete artefact rows after their dependents have been removed.

    Breaks the self-referential parent FK before deleting.
    """
    Artefact.query.filter(Artefact.id.in_(artefact_ids)).update(
        {Artefact.parent_artefact_id: None}, synchronize_session=False)
    Artefact.query.filter(Artefact.id.in_(artefact_ids)).delete(synchronize_session=False)


def _analysis_file_path(analysis, hint_file_map: dict) -> str | None:
    """Return the file/dir path for an analysis that operates on a specific
    extracted file, or None for analyses that have no path context.

    Path-bearing analyses:
      ARCHIVE_EXTRACT  — path of the archive file being extracted
                         (from hint file_id → ExtractedFile.path)
      ARCHIVE_DETECT   — path_prefix of the archive being scanned for
                         nested archives (empty → top-level, returns None)
      FORMAT_CONVERT   — path_prefix of the archive being converted
                         (empty → direct artefact convert, returns None)
      RISCOS_MODULE_PARSE — path_prefix of the archive context
                         (empty → top-level scan, returns None)
    """
    import json as _json
    from arcology_shared.enums import AnalysisType as _AT

    if not analysis.hints:
        return None
    try:
        h = _json.loads(analysis.hints)
    except Exception:
        return None

    atype = analysis.analysis_type
    if atype == _AT.ARCHIVE_EXTRACT:
        fid = h.get(HintKey.FILE_ID)
        if fid and fid in hint_file_map:
            return hint_file_map[fid]['path']
        return None
    if atype in (_AT.ARCHIVE_DETECT, _AT.FORMAT_CONVERT, _AT.RISCOS_MODULE_PARSE):
        prefix = h.get(HintKey.PATH_PREFIX, '')
        return prefix if prefix else None
    return None


def _build_file_path_tree(path_analyses: list[tuple[str, object]]) -> dict:
    """Build a nested dict tree from [(path, analysis), ...].

    Each node: {'children': {name: node, ...}, 'analyses': [Analysis, ...]}
    The root node's children are the top-level path components.  Children
    dicts preserve insertion order and are later sorted by the template.
    """
    root: dict = {'children': {}, 'analyses': []}
    for path, analysis in path_analyses:
        parts = [p for p in path.split('/') if p]
        node = root
        for part in parts:
            if part not in node['children']:
                node['children'][part] = {'children': {}, 'analyses': []}
            node = node['children'][part]
        node['analyses'].append(analysis)
    return root


def build_processing_tree(root: Artefact) -> tuple[dict, bool, dict, int]:
    """Build a nested tree structure for the processing tree view.

    Returns (tree_node, has_active_analyses, status_counts, total_count).
    Each tree_node is a dict:
      {
        'artefact':   Artefact,
        'analyses':   [Analysis, ...],   # non-path analyses (flat list)
        'path_tree':  dict | None,       # nested path tree (see _build_file_path_tree)
        'children':   [node, ...],       # derived artefact child nodes
      }

    Analyses that have a file-path context (ARCHIVE_EXTRACT with a file_id,
    ARCHIVE_DETECT / FORMAT_CONVERT with a path_prefix) are separated out of
    the flat 'analyses' list and placed in 'path_tree' so the template can
    render them as a hierarchical file-path tree.

    All data is fetched in flat queries (no N+1) and assembled in Python.
    """
    import json as _json
    from collections import defaultdict
    from arcology_shared.enums import AnalysisType as _AT

    all_ids = [root.id] + get_all_derived_artefact_ids(root)

    # Exclude any derived artefact queued for deletion so it disappears from the
    # processing tree of its still-visible parent (the root itself is already
    # 404-gated by can_view_artefact when pending).
    all_artefacts = Artefact.query.filter(
        Artefact.id.in_(all_ids),
        Artefact.pending_deletion.is_(False),
    ).all()
    artefact_map = {a.id: a for a in all_artefacts}

    children_map: dict[int, list] = defaultdict(list)
    for a in all_artefacts:
        if a.parent_artefact_id is not None:
            children_map[a.parent_artefact_id].append(a)

    all_analyses = (
        Analysis.query
        .filter(Analysis.artefact_id.in_(all_ids))
        .order_by(Analysis.id)
        .all()
    )

    analyses_map: dict[int, list] = defaultdict(list)
    for analysis in all_analyses:
        analyses_map[analysis.artefact_id].append(analysis)

    has_active = any(
        a.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)
        for a in all_analyses
    )

    status_counts = {s.value: 0 for s in AnalysisStatus}
    for a in all_analyses:
        status_counts[a.status.value] += 1
    total_count = len(all_analyses)

    # Resolve ARCHIVE_EXTRACT file_ids → ExtractedFile paths in one query.
    file_ids = []
    for analysis in all_analyses:
        if analysis.analysis_type == _AT.ARCHIVE_EXTRACT and analysis.hints:
            try:
                fid = _json.loads(analysis.hints).get(HintKey.FILE_ID)
                if fid:
                    file_ids.append(fid)
            except Exception:
                pass

    hint_file_map: dict[int, dict] = {}
    if file_ids:
        rows = (
            ExtractedFile.query
            .filter(ExtractedFile.id.in_(file_ids))
            .with_entities(ExtractedFile.id, ExtractedFile.path, ExtractedFile.filename)
            .all()
        )
        hint_file_map = {r.id: {'path': r.path, 'filename': r.filename} for r in rows}

    def _build(aid: int) -> dict:
        plain: list = []
        path_items: list[tuple[str, object]] = []
        for a in analyses_map.get(aid, []):
            p = _analysis_file_path(a, hint_file_map)
            if p is not None:
                path_items.append((p, a))
            else:
                plain.append(a)
        return {
            'artefact': artefact_map[aid],
            'analyses': plain,
            'path_tree': _build_file_path_tree(path_items) if path_items else None,
            'children': [
                _build(c.id)
                for c in sorted(children_map.get(aid, []), key=lambda x: x.id)
            ],
        }

    return _build(root.id), has_active, status_counts, total_count


def reset_artefact_for_reanalysis(artefact: Artefact, commit: bool = True):
    """
    Reset an artefact to its just-uploaded state ready for re-analysis.

    Deletes all analyses, derived artefacts, and partitions (with their
    extracted file listings) for this artefact, then removes the associated
    files from disk.  The artefact's own uploaded file is preserved.

    This must be called before queueing new analyses when the user triggers
    a re-analyse, so that stale results from previous runs are fully cleared.

    Pass commit=False to defer the commit to the caller (useful for batch
    operations).  The caller must call db.session.commit() afterwards.
    """
    cleanup = _collect_cleanup_paths_for_artefact(artefact, 'reset')

    # Collect all derived artefact IDs first so blob ref-counting is accurate
    # when sibling artefacts share the same blob object.
    all_derived_ids = get_all_derived_artefact_ids(artefact)

    # Delete storage files for all derived artefacts (recursively).
    # Must happen before the DB delete so we can still walk the ORM tree.
    deleting_ids: set = set(all_derived_ids)
    processed_blobs: set = set()
    for derived in artefact.derived_artefacts:
        delete_artefact_files(derived, deleting_ids=deleting_ids, processed_blobs=processed_blobs)

    # Collect all artefact IDs to clean (derived + root) for bulk operations.
    all_ids = all_derived_ids + [artefact.id]

    # Null out derived_from_analysis_id before deleting analyses, to avoid
    # FK violations from artefacts -> analyses.
    if all_derived_ids:
        Artefact.query.filter(Artefact.id.in_(all_derived_ids)).update(
            {Artefact.derived_from_analysis_id: None}, synchronize_session=False)

    bulk_delete_artefact_dependents(all_ids)

    # Clear analysis-derived metadata on the root artefact (e.g. the
    # ISO 9660 Primary Volume Descriptor written by METADATA_EXTRACT).
    # Without this, stale volume info survives a reset and is shown
    # alongside the freshly queued analyses until they finish — and
    # persists indefinitely if METADATA_EXTRACT is no longer queued
    # (e.g. the artefact type was changed away from ISO).
    Artefact.query.filter(Artefact.id == artefact.id).update(
        {Artefact.media_metadata: None}, synchronize_session=False)

    # Delete derived artefacts: break self-referential FK, then delete.
    if all_derived_ids:
        bulk_delete_artefacts(all_derived_ids)

    if commit:
        db.session.commit()

    return cleanup


def cleanup_analysis_outputs(output_folder, output_files, output_dirs, cache_dir, logger):
    """Delete analysis output files and directories.

    Designed to run in a background daemon thread; all paths are passed as
    plain strings so no ORM session or Flask app context is required.
    """
    real_output = os.path.realpath(output_folder)

    def _is_safe(p: str) -> bool:
        """Return True only if p resolves to a path inside output_folder."""
        return os.path.realpath(p).startswith(real_output + os.sep)

    def _prune_empty_parents(path: str) -> None:
        """Remove empty parent directories up to, but not including, output_folder."""
        current = os.path.dirname(os.path.realpath(path))
        while current.startswith(real_output + os.sep):
            try:
                os.rmdir(current)
                logger.info(f"Deleted empty parent directory: {current}")
            except OSError:
                break
            current = os.path.dirname(current)

    # Remove named output files (e.g., flux visualisation PNGs).
    for filename in output_files:
        path = os.path.join(output_folder, filename)
        if not _is_safe(path):
            logger.warning(f"Skipping out-of-bounds output file: {filename!r}")
            continue
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Deleted output file: {filename}")
            except Exception as e:
                logger.warning(f"Failed to delete output file {filename}: {e}")

    # Remove extraction output directories (e.g., extracted disc file trees).
    for path in output_dirs:
        local_path = map_output_path_to_local_root(path, output_folder)
        if not _is_safe(local_path):
            logger.warning(f"Skipping out-of-bounds output directory: {path!r}")
            continue
        if os.path.exists(local_path):
            try:
                shutil.rmtree(local_path)
                logger.info(f"Deleted output directory: {local_path}")
                _prune_empty_parents(local_path)
            except Exception as e:
                logger.warning(f"Failed to delete output directory {path}: {e}")

    # Remove cached decompressed partition images created by PARTITION_DETECT.
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            logger.info(f"Deleted partition cache: {cache_dir}")
        except Exception as e:
            logger.warning(f"Failed to delete partition cache {cache_dir}: {e}")


def _collect_cleanup_paths_for_artefact(artefact: Artefact, context: str = 'cleanup') -> dict[str, list[str] | str]:
    """Collect output files/directories/cache paths for an artefact tree."""
    output_folder = get_output_folder()
    all_analyses = collect_all_analyses(artefact)
    output_dirs = [a.output_path for a in all_analyses if a.output_path]
    output_files = []

    for analysis in all_analyses:
        if analysis.details:
            try:
                details = json.loads(analysis.details)
                if 'outputs' in details and isinstance(details['outputs'], list):
                    for output in details['outputs']:
                        if 'filename' in output:
                            output_files.append(output['filename'])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                current_app.logger.warning(f"Failed to parse analysis details during {context}: {e}")

    cache_dir = os.path.join(output_folder, '.cache', artefact.uuid)
    return {
        'output_folder': output_folder,
        'output_files': output_files,
        'output_dirs': output_dirs,
        'cache_dir': cache_dir,
    }


def cleanup_artefact_outputs(artefact: Artefact, logger) -> None:
    """Delete derived output files/directories for an artefact tree."""
    cleanup = _collect_cleanup_paths_for_artefact(artefact)
    cleanup_analysis_outputs(
        cleanup['output_folder'],
        cleanup['output_files'],
        cleanup['output_dirs'],
        cleanup['cache_dir'],
        logger,
    )


def delete_artefact_files(artefact, deleting_ids=None, processed_blobs=None):
    """Recursively delete files for an artefact and all its derived artefacts.

    When blob deduplication is in use, ``deleting_ids`` should be the full set
    of artefact IDs being deleted in this operation so the function can tell
    whether a shared blob has external references that must be preserved.
    ``processed_blobs`` is an internal set used to avoid double-processing a
    shared blob across sibling artefacts; callers should leave it as None.
    """
    storage = current_app.storage
    if deleting_ids is None:
        def _collect_ids(node):
            ids = {node.id}
            for child in node.derived_artefacts:
                ids.update(_collect_ids(child))
            return ids
        deleting_ids = _collect_ids(artefact)
    if processed_blobs is None:
        processed_blobs = set()

    for derived in artefact.derived_artefacts:
        delete_artefact_files(derived, deleting_ids, processed_blobs)

    blob = artefact_blob(artefact)
    if blob is not None:
        blob_key = (artefact.storage_directory, blob.id)
        if blob_key in processed_blobs:
            return
        processed_blobs.add(blob_key)
        # Don't delete the physical file or blob record if any artefact
        # *outside* the deletion set still references this blob.
        external_reference = (
            db.session.query(Artefact.id)
            .filter(
                (Artefact.upload_blob_id == blob.id)
                if artefact.upload_blob_id is not None
                else (Artefact.output_blob_id == blob.id),
                ~Artefact.id.in_(deleting_ids),
            )
            .first()
        )
        if external_reference is not None:
            return

    try:
        key = get_artefact_storage_key(artefact)
        storage.delete(key)
        if blob is not None:
            db.session.delete(blob)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete file for artefact {artefact.uuid}: {e}")


def cleanup_artefact_outputs_s3(artefact, storage):
    """Clean up analysis outputs for an artefact using S3 storage.

    Deletes output files, output directories (extraction trees), and
    cached partition images via the S3 storage backend.
    """
    cleanup = _collect_cleanup_paths_for_artefact(artefact)
    for filename in cleanup['output_files']:
        try:
            key = storage.storage_key('outputs', filename)
            storage.delete(key)
            current_app.logger.info(f"Deleted output file: {filename}")
        except Exception as e:
            current_app.logger.warning(f"Failed to delete output file {filename}: {e}")
    for path in cleanup['output_dirs']:
        try:
            if os.path.isabs(path):
                parts = path.rstrip('/').split('/')
                try:
                    idx = parts.index('outputs')
                    rel = '/'.join(parts[idx + 1:])
                except ValueError:
                    rel = '/'.join(parts[-3:]) if len(parts) >= 3 else parts[-1]
            else:
                rel = path
            prefix = storage.storage_key('outputs', rel)
            storage.delete_prefix(prefix)
        except Exception as e:
            current_app.logger.warning(f"Failed to delete output directory {path}: {e}")
    try:
        cache_prefix = storage.storage_key('outputs', f'.cache/{artefact.uuid}')
        storage.delete_prefix(cache_prefix)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete partition cache for artefact {artefact.uuid}: {e}")


def delete_item_files(item):
    """Delete all files associated with an item's artefacts before DB cascade delete.

    For each artefact, removes the stored file (recursing into derived artefacts),
    analysis output directories, named output files, and cached partition images.
    Must be called while the ORM relationships are still intact (before db.session.delete).
    """
    storage = current_app.storage
    from arcology_shared.storage import S3Storage

    for artefact in item.artefacts:
        # Delete stored files for this artefact and all its derived artefacts.
        delete_artefact_files(artefact)
        if isinstance(storage, S3Storage):
            cleanup_artefact_outputs_s3(artefact, storage)
        else:
            cleanup_artefact_outputs(artefact, current_app.logger)


class ArtefactMoveError(ValueError):
    """An artefact move is not permitted or not possible.

    ``code`` identifies the failed precondition so callers can map it to
    their own presentation (flash category + redirect for the web route,
    HTTP status for the API route):

      - ``'source_forbidden'`` — caller may not move artefacts out of the
        (private) source item.
      - ``'not_root'``         — only root artefacts can be moved.
      - ``'target_forbidden'`` — caller may not move artefacts into the
        target item (includes the curator publish-prevention rule).
      - ``'same_item'``        — artefact is already in the target item.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_artefact_move(artefact, target_item, user, *, sees_all: bool = False):
    """Raise ArtefactMoveError unless *user* may move *artefact* into *target_item*.

    Single source of truth for the move preconditions shared by the web and
    API move routes.  The caller is responsible for resolving and
    visibility-checking *target_item* first (a non-visible target must be
    indistinguishable from a nonexistent one, which is presentation-specific).

    The curator publish-prevention rule lives here: a curator on a private
    source item (someone with contribute access but not owner/admin) must not
    be able to move artefacts into a public item, as that would silently
    publish private content.  Whenever the source is private and the caller is
    acting as a curator — or the target itself is private — write access on
    the target is required.
    """
    if artefact.item.private_effective and not can_contribute_to_item(
            artefact.item, user, sees_all=sees_all):
        raise ArtefactMoveError(
            'source_forbidden', 'Not permitted to move artefacts from this item')

    if artefact.parent_artefact_id is not None:
        raise ArtefactMoveError('not_root', 'Only root artefacts can be moved')

    curator_on_source = (artefact.item.private_effective
                         and not can_change_owner(artefact.item, user))
    if (curator_on_source or target_item.private_effective) and \
            not can_contribute_to_item(target_item, user, sees_all=sees_all):
        raise ArtefactMoveError(
            'target_forbidden', 'Not permitted to move artefacts into this item')

    if target_item.id == artefact.item_id:
        raise ArtefactMoveError('same_item', 'Artefact is already in that item')


def move_artefact_to_item(artefact, new_item):
    """Move a root artefact (and all its derived artefacts) to a different item.

    Only root artefacts (parent_artefact_id is None) may be moved.
    Slug uniqueness is re-checked in the target item; slugs are regenerated on
    collision.  This is a pure DB operation — no files move on disk.
    """
    if artefact.parent_artefact_id is not None:
        raise ValueError('Only root artefacts can be moved')
    if artefact.item_id == new_item.id:
        raise ValueError('Artefact is already in this item')

    def _update_item_id(art, target_item_id):
        art.item_id = target_item_id
        # Ensure slug is unique within the target item
        art.slug = ensure_unique_slug(
            art.slug, Artefact, existing_id=art.id,
            scope_filter={'item_id': target_item_id},
        )
        for derived in art.derived_artefacts:
            _update_item_id(derived, target_item_id)

    _update_item_id(artefact, new_item.id)
    db.session.commit()


def _collect_item_artefact_ids(item_ids):
    """Collect all artefact IDs for a list of items (direct + all derived) via CTE."""
    direct_ids = [r[0] for r in db.session.execute(
        select(Artefact.id).where(Artefact.item_id.in_(item_ids))
    ).all()]
    if not direct_ids:
        return []

    # Recursive CTE: find all derived artefacts from any artefact in this item
    base = select(Artefact.id).where(Artefact.parent_artefact_id.in_(direct_ids))
    cte = base.cte(name='item_derived', recursive=True)
    recursive = select(Artefact.id).where(Artefact.parent_artefact_id == cte.c.id)
    cte = cte.union_all(recursive)
    derived_ids = [r[0] for r in db.session.execute(select(cte.c.id)).all()]

    return direct_ids + derived_ids


def _transcode_blobs_externally_referenced(blob_ids, deleting_ids):
    """Subset of *blob_ids* still referenced by a SURVIVING artefact.

    A content-addressed transcode output is shared across every artefact holding
    the identical source media, referenced via ``Artefact.output_blob_id`` (rare)
    or a ``ReplayMovie``/``MediaFile`` transcode FK.  References from rows owned
    by the artefacts being deleted (``deleting_ids``) cascade away with them and
    do not count; a reference from any other (surviving) artefact means the bytes
    must be kept.

    Resolved in a fixed three queries over the whole candidate set rather than
    per-blob, so the cost is independent of how many shared outputs an item has.
    """
    if not blob_ids:
        return set()
    blob_ids = set(blob_ids)
    referenced: set = set()
    referenced.update(db.session.scalars(
        select(Artefact.output_blob_id).where(
            Artefact.output_blob_id.in_(blob_ids),
            ~Artefact.id.in_(deleting_ids))
    ).all())
    for model in (ReplayMovie, MediaFile):
        rows = db.session.execute(
            select(model.mp4_output_blob_id, model.poster_blob_id).where(
                or_(model.mp4_output_blob_id.in_(blob_ids),
                    model.poster_blob_id.in_(blob_ids)),
                ~model.artefact_id.in_(deleting_ids))
        ).all()
        for mp4_blob_id, poster_blob_id in rows:
            if mp4_blob_id in blob_ids:
                referenced.add(mp4_blob_id)
            if poster_blob_id in blob_ids:
                referenced.add(poster_blob_id)
    return referenced


def _collect_item_cleanup_keys(all_artefact_ids):
    """Collect all storage keys and cache prefixes for cleanup.

    Returns a dict with keys: artefact_keys, output_file_keys, output_dir_prefixes,
    cache_prefixes, upload_blob_ids, output_blob_ids.  The blob ID lists contain
    IDs of blobs that have no external references (safe to delete).
    """
    artefact_keys = []
    cache_prefixes = []
    output_dir_prefixes = []
    output_file_keys = []

    if not all_artefact_ids:
        return {
            HintKey.ARTEFACT_KEYS: artefact_keys,
            HintKey.OUTPUT_FILE_KEYS: output_file_keys,
            HintKey.OUTPUT_DIR_PREFIXES: output_dir_prefixes,
            HintKey.CACHE_PREFIXES: cache_prefixes,
            'upload_blob_ids': [],
            'output_blob_ids': [],
        }

    # Single query for artefact storage keys, blob references, and cache prefixes
    rows = db.session.execute(
        select(
            Artefact.id,
            Artefact.storage_directory,
            Artefact.storage_path,
            Artefact.uuid,
            Artefact.upload_blob_id,
            Artefact.output_blob_id,
        )
        .where(Artefact.id.in_(all_artefact_ids))
    ).all()
    deleting_ids = set(all_artefact_ids)
    upload_blob_ids = set()
    output_blob_ids = set()
    for _artefact_id, storage_dir, storage_path, artefact_uuid, upload_blob_id, output_blob_id in rows:
        if upload_blob_id is not None:
            upload_blob_ids.add(upload_blob_id)
        elif output_blob_id is not None:
            output_blob_ids.add(output_blob_id)
        elif storage_path:
            # Legacy artefact with no blob record: include directly
            directory = 'outputs' if storage_dir == StorageDirectory.OUTPUTS else 'uploads'
            artefact_keys.append(f"{directory}/{storage_path}")
        cache_prefixes.append(f"outputs/.cache/{artefact_uuid}")

    # For blobs, only include those with no external references
    orphan_upload_blob_ids = []
    orphan_output_blob_ids = []
    for model, ids, fk_column, directory, orphan_ids in (
        (UploadBlob, upload_blob_ids, Artefact.upload_blob_id, 'uploads', orphan_upload_blob_ids),
        (OutputBlob, output_blob_ids, Artefact.output_blob_id, 'outputs', orphan_output_blob_ids),
    ):
        for blob in (model.query.filter(model.id.in_(ids)).all() if ids else []):
            external_ref = (
                db.session.query(Artefact.id)
                .filter(fk_column == blob.id, ~Artefact.id.in_(deleting_ids))
                .first()
            )
            if external_ref is None:
                artefact_keys.append(f"{directory}/{blob.storage_path}")
                orphan_ids.append(blob.id)

    # Content-addressed transcode outputs are shared, refcounted OutputBlobs
    # referenced via ReplayMovie/MediaFile FKs (not Artefact.output_blob_id), so
    # the loop above never sees them and the per-artefact output_dir_prefixes
    # sweep deliberately misses them (they live under outputs/media/<hash>/...).
    # Gather the blobs referenced by the artefacts being deleted and orphan only
    # those with no surviving reference of any kind.
    transcode_blob_ids = set()
    for model in (ReplayMovie, MediaFile):
        for mp4_blob_id, poster_blob_id in db.session.execute(
            select(model.mp4_output_blob_id, model.poster_blob_id)
            .where(model.artefact_id.in_(all_artefact_ids))
        ).all():
            if mp4_blob_id is not None:
                transcode_blob_ids.add(mp4_blob_id)
            if poster_blob_id is not None:
                transcode_blob_ids.add(poster_blob_id)
    transcode_blob_ids -= set(orphan_output_blob_ids)
    survivors = _transcode_blobs_externally_referenced(
        transcode_blob_ids, deleting_ids)
    orphaned_transcode_ids = transcode_blob_ids - survivors
    for blob in (OutputBlob.query.filter(
            OutputBlob.id.in_(orphaned_transcode_ids)).all()
            if orphaned_transcode_ids else []):
        artefact_keys.append(f"outputs/{blob.storage_path}")
        orphan_output_blob_ids.append(blob.id)

    # Analysis output dirs and named output files
    rows = db.session.execute(
        select(Analysis.output_path, Analysis.details)
        .where(Analysis.artefact_id.in_(all_artefact_ids))
    ).all()
    for output_path, details in rows:
        if output_path:
            # output_path may be absolute (/data/outputs/...) or relative;
            # normalise to a storage key prefix under 'outputs/'.
            if os.path.isabs(output_path):
                parts = output_path.rstrip('/').split('/')
                try:
                    idx = parts.index('outputs')
                    rel = '/'.join(parts[idx + 1:])
                except ValueError:
                    rel = '/'.join(parts[-3:]) if len(parts) >= 3 else parts[-1]
            else:
                rel = output_path
            output_dir_prefixes.append(f"outputs/{rel}")
        if details:
            try:
                parsed = json.loads(details)
                if 'outputs' in parsed and isinstance(parsed['outputs'], list):
                    for output in parsed['outputs']:
                        if 'filename' in output:
                            output_file_keys.append(f"outputs/{output['filename']}")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    return {
        HintKey.ARTEFACT_KEYS: artefact_keys,
        HintKey.OUTPUT_FILE_KEYS: output_file_keys,
        HintKey.OUTPUT_DIR_PREFIXES: output_dir_prefixes,
        HintKey.CACHE_PREFIXES: cache_prefixes,
        'upload_blob_ids': orphan_upload_blob_ids,
        'output_blob_ids': orphan_output_blob_ids,
    }


def _collect_all_item_ids(item):
    """Collect the item's ID and all descendant item IDs via recursive CTE."""
    base = select(Item.id).where(Item.parent_id == item.id)
    cte = base.cte(name='item_descendants', recursive=True)
    recursive = select(Item.id).where(Item.parent_id == cte.c.id)
    cte = cte.union_all(recursive)
    descendant_ids = [r[0] for r in db.session.execute(select(cte.c.id)).all()]
    return [item.id] + descendant_ids


def queue_storage_cleanup(cleanup_keys: dict, artefact_id: int | None = None,
                          commit: bool = False,
                          priority: int = ANALYSIS_PRIORITY_NORMAL):
    """Queue a CLEANUP analysis job that deletes the given storage keys.

    *cleanup_keys* is the dict produced by _collect_item_cleanup_keys():
    {'artefact_keys': [...], 'output_file_keys': [...],
     'output_dir_prefixes': [...], 'cache_prefixes': [...]}.

    The worker's process_cleanup handler deletes each key/prefix via its
    storage backend, so the cleanup works for both local and S3 deployments,
    survives web-container restarts (unlike the previous daemon threads), and
    is retryable through the normal analysis queue machinery.

    *priority* controls where the job sits in the worker's queue (which is
    ordered priority DESC, created_at).  Item deletion can leave it at the
    default NORMAL priority — nothing races a deleted item.  A *re-analysis*
    cleanup, however, MUST outrank the replacement analyses queued alongside
    it: otherwise the worker drains the higher-priority new jobs first, leaving
    the CLEANUP PENDING, and it then deletes the per-artefact partition cache
    (outputs/.cache/<uuid>, shared across runs) out from under the fresh run.
    Pass a priority above the new analyses' priority in that case.

    Returns the Analysis row, or None when there is nothing to delete.
    The caller commits (pass commit=True to commit here) — for deletions this
    lets the job row become part of the same transaction as the DB deletes,
    so a job exists if and only if the deletion committed.
    """
    if not any(cleanup_keys.get(k) for k in (
            HintKey.ARTEFACT_KEYS, HintKey.OUTPUT_FILE_KEYS,
            HintKey.OUTPUT_DIR_PREFIXES, HintKey.CACHE_PREFIXES)):
        return None

    job = Analysis(
        artefact_id=artefact_id,
        analysis_type=AnalysisType.CLEANUP,
        status=AnalysisStatus.PENDING,
        hints=json.dumps(cleanup_keys),
        priority=priority,
    )
    db.session.add(job)
    if commit:
        db.session.commit()
    return job


def collect_output_cleanup_keys(artefact: Artefact) -> dict:
    """Storage keys for an artefact tree's analysis outputs (not its uploads).

    Used before reset_artefact_for_reanalysis() — which deletes the Analysis
    rows the keys are derived from — to queue a CLEANUP job for the previous
    run's outputs.  artefact_keys is emptied: a re-analyse must never delete
    the uploaded originals (the derived artefacts' stored files are deleted
    synchronously inside the reset).
    """
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    keys = _collect_item_cleanup_keys(all_ids)
    keys[HintKey.ARTEFACT_KEYS] = []
    return keys


def _delete_artefact_subtree(all_ids, cleanup, *, batch_size=None,
                             heartbeat=_noop_heartbeat,
                             check_cancelled=_noop_check_cancelled,
                             commit=False) -> int:
    """Delete all artefact rows in *all_ids* plus their dependents and blobs.

    Shared Phase-4 deletion used by both the synchronous bulk paths and the
    task-runner job drivers.  *cleanup* is the dict from
    ``_collect_item_cleanup_keys`` (for the orphan blob id lists).  Returns the
    number of extracted files deleted.
    """
    if not all_ids:
        return 0
    # Null out derived_from_analysis_id before deleting analyses.
    Artefact.query.filter(Artefact.id.in_(all_ids)).update(
        {Artefact.derived_from_analysis_id: None}, synchronize_session=False)
    if commit:
        db.session.commit()
    deleted = bulk_delete_artefact_dependents(
        all_ids, batch_size=batch_size, heartbeat=heartbeat,
        check_cancelled=check_cancelled, commit=commit)
    bulk_delete_artefacts(all_ids)
    if cleanup['upload_blob_ids']:
        UploadBlob.query.filter(
            UploadBlob.id.in_(cleanup['upload_blob_ids'])
        ).delete(synchronize_session=False)
    if cleanup['output_blob_ids']:
        OutputBlob.query.filter(
            OutputBlob.id.in_(cleanup['output_blob_ids'])
        ).delete(synchronize_session=False)
    if commit:
        db.session.commit()
    return deleted


def bulk_delete_item(item):
    """Delete an item, its descendants, and all related records using bulk SQL.

    Handles item hierarchy (parent/child items) by collecting all descendant
    items first, then bulk-deleting all artefacts across the entire tree.

    Replaces the previous approach of ORM cascade delete which loaded every
    related object into memory and emitted individual DELETEs — too slow for
    items with thousands of artefacts.

    Synchronous (single transaction).  For large items prefer the asynchronous
    path (``mark_item_pending_deletion`` + ``queue_item_delete`` →
    ``run_item_delete_job`` on the task runner); this function remains for the
    CLI and small items.

    File cleanup is queued as a CLEANUP job in the same transaction as the
    deletes; the worker performs the storage deletions.
    """
    # Phase 1: Collect all item IDs (this item + all descendants)
    all_item_ids = _collect_all_item_ids(item)

    # Phase 2: Collect all artefact IDs via CTE
    all_ids = _collect_item_artefact_ids(all_item_ids)

    # Phase 3: Collect storage keys before deleting DB records
    cleanup = _collect_item_cleanup_keys(all_ids)

    # Phase 4: Bulk SQL deletes in FK-safe order
    _delete_artefact_subtree(all_ids, cleanup)

    # Phase 5: item-level rows + queue worker-side file cleanup, all committed
    # atomically with the deletes.
    _delete_item_rows(all_item_ids)
    queue_storage_cleanup(cleanup)
    db.session.commit()


def _delete_item_rows(all_item_ids):
    """Delete the Item rows themselves (and their item-level children).

    Does NOT commit — the caller controls the transaction boundary.
    """
    ExternalReference.query.filter(
        ExternalReference.item_id.in_(all_item_ids)
    ).delete(synchronize_session=False)
    db.session.execute(
        item_tags.delete().where(item_tags.c.item_id.in_(all_item_ids)))
    # Break item self-referential FK, then delete all items.
    Item.query.filter(Item.id.in_(all_item_ids)).update(
        {Item.parent_id: None}, synchronize_session=False)
    Item.query.filter(Item.id.in_(all_item_ids)).delete(
        synchronize_session=False)


# =============================================================================
# Asynchronous deletion (task-runner control-plane jobs)
#
# The web request flags the target subtree pending_deletion (so it vanishes
# from every visibility surface immediately) and queues an ITEM_DELETE /
# ARTEFACT_DELETE analysis; the task runner then batch-deletes the rows here.
# =============================================================================

def _cancel_pending_subtree_analyses(artefact_ids, *, reason):
    """Fail any PENDING analyses for *artefact_ids* so the worker can't claim a
    job on an artefact whose rows are about to be torn down.  A RUNNING analysis
    already claimed is harmless: the worker's result write-back keys on
    analysis_id and affects 0 rows once the row is gone.  *reason* is the
    user-visible error_message recorded on the cancelled analyses."""
    if not artefact_ids:
        return
    Analysis.query.filter(
        Analysis.artefact_id.in_(artefact_ids),
        Analysis.status == AnalysisStatus.PENDING,
    ).update({Analysis.status: AnalysisStatus.FAILED,
              Analysis.error_message: reason},
             synchronize_session=False)


def mark_item_pending_deletion(item, *, commit=False):
    """Flag *item* and its whole descendant subtree pending_deletion.

    One bulk UPDATE — cheap, so the web request returns immediately while the
    task runner does the heavy row deletion.  Also fails any PENDING analyses in
    the subtree so the worker stops being handed the dying artefacts.
    """
    all_item_ids = _collect_all_item_ids(item)
    Item.query.filter(Item.id.in_(all_item_ids)).update(
        {Item.pending_deletion: True}, synchronize_session=False)
    _cancel_pending_subtree_analyses(
        _collect_item_artefact_ids(all_item_ids),
        reason='cancelled: item is being deleted')
    if commit:
        db.session.commit()


def mark_artefact_pending_deletion(artefact, *, commit=False):
    """Flag *artefact* and its derived subtree pending_deletion (see above)."""
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    Artefact.query.filter(Artefact.id.in_(all_ids)).update(
        {Artefact.pending_deletion: True}, synchronize_session=False)
    _cancel_pending_subtree_analyses(
        all_ids, reason='cancelled: artefact is being deleted')
    if commit:
        db.session.commit()


def queue_item_delete(item, *, commit=False, priority=ANALYSIS_PRIORITY_NORMAL):
    """Queue an ITEM_DELETE control-plane job carrying the item id in hints.

    artefact_id is NULL (the item outlives no single artefact), so the job is
    never gated by the per-artefact CLEANUP barrier.
    """
    job = Analysis(
        artefact_id=None,
        analysis_type=AnalysisType.ITEM_DELETE,
        status=AnalysisStatus.PENDING,
        hints=json.dumps({'item_id': item.id}),
        priority=priority,
    )
    db.session.add(job)
    if commit:
        db.session.commit()
    return job


def queue_artefact_delete(artefact, *, commit=False,
                          priority=ANALYSIS_PRIORITY_NORMAL):
    """Queue an ARTEFACT_DELETE control-plane job (artefact id in hints)."""
    job = Analysis(
        artefact_id=None,
        analysis_type=AnalysisType.ARTEFACT_DELETE,
        status=AnalysisStatus.PENDING,
        hints=json.dumps({'artefact_id': artefact.id}),
        priority=priority,
    )
    db.session.add(job)
    if commit:
        db.session.commit()
    return job


def run_item_delete_job(item, *, heartbeat=_noop_heartbeat,
                        check_cancelled=_noop_check_cancelled) -> dict:
    """Delete an item subtree's DB rows in committed, heartbeated batches.

    The control-plane (task-runner) counterpart of ``bulk_delete_item``.  The
    item is already flagged pending_deletion (and hidden everywhere), so this
    can take its time.  Re-runnable: each step re-collects whatever survives, so
    a stale-reset re-claim simply finishes the job (deletion is idempotent).
    """
    all_item_ids = _collect_all_item_ids(item)
    all_ids = _collect_item_artefact_ids(all_item_ids)
    cleanup = _collect_item_cleanup_keys(all_ids)

    deleted = _delete_artefact_subtree(
        all_ids, cleanup, batch_size=EXTRACTED_FILE_DELETE_BATCH,
        heartbeat=heartbeat, check_cancelled=check_cancelled, commit=True)

    _delete_item_rows(all_item_ids)
    queue_storage_cleanup(cleanup)
    db.session.commit()
    return {
        'summary': f'{deleted} extracted file(s), {len(all_ids)} artefact(s), '
                   f'{len(all_item_ids)} item(s) deleted',
        'extracted_files': deleted,
        'artefacts': len(all_ids),
        'items': len(all_item_ids),
    }


def run_artefact_delete_job(artefact, *, heartbeat=_noop_heartbeat,
                            check_cancelled=_noop_check_cancelled) -> dict:
    """Delete an artefact + derived subtree's DB rows in batches (task runner).

    The control-plane counterpart of the synchronous artefact delete.  Leaves
    the enclosing item intact.  Re-runnable like ``run_item_delete_job``.
    """
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    cleanup = _collect_item_cleanup_keys(all_ids)

    deleted = _delete_artefact_subtree(
        all_ids, cleanup, batch_size=EXTRACTED_FILE_DELETE_BATCH,
        heartbeat=heartbeat, check_cancelled=check_cancelled, commit=True)

    queue_storage_cleanup(cleanup)
    db.session.commit()
    return {
        'summary': f'{deleted} extracted file(s), {len(all_ids)} artefact(s) deleted',
        'extracted_files': deleted,
        'artefacts': len(all_ids),
    }
# vim: ts=4 sw=4 et
