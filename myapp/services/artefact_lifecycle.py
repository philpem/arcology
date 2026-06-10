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
import threading
from flask import current_app
from sqlalchemy import select
from ..database import (
    Analysis,
    AnalysisStatus,
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    ArtefactRestriction,
    ExternalReference,
    ExtractedFile,
    ExtractedFileRestriction,
    Item,
    Partition,
    RecognisedProduct,
    RiscosModule,
    StorageDirectory,
    artefact_tags,
    item_tags,
)
from ..extensions import db
from ..utils.slugs import ensure_unique_slug
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


def bulk_delete_artefact_dependents(artefact_ids: list[int]):
    """Bulk-delete all referencing rows across every FK table for the given artefact IDs.

    Deletes analyses, extracted file restrictions, recognised products,
    extracted files, partitions, protection/mastering/module records,
    artefact restrictions, and tag associations.  Does NOT delete the
    artefacts themselves — call ``bulk_delete_artefacts`` for that.
    """
    Analysis.query.filter(Analysis.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    partition_subq = db.session.query(Partition.id).filter(
        Partition.artefact_id.in_(artefact_ids)).subquery()
    ef_subq = db.session.query(ExtractedFile.id).filter(
        ExtractedFile.partition_id.in_(db.session.query(partition_subq.c.id))).subquery()
    ExtractedFileRestriction.query.filter(
        ExtractedFileRestriction.extracted_file_id.in_(
            db.session.query(ef_subq.c.id)
        )).delete(synchronize_session=False)
    RecognisedProduct.query.filter(
        RecognisedProduct.partition_id.in_(
            db.session.query(partition_subq.c.id)
        )).delete(synchronize_session=False)
    ExtractedFile.query.filter(
        ExtractedFile.partition_id.in_(
            db.session.query(partition_subq.c.id)
        )).delete(synchronize_session=False)
    Partition.query.filter(Partition.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactProtection.query.filter(ArtefactProtection.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactMastering.query.filter(ArtefactMastering.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    RiscosModule.query.filter(RiscosModule.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactRestriction.query.filter(ArtefactRestriction.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    db.session.execute(artefact_tags.delete().where(artefact_tags.c.artefact_id.in_(artefact_ids)))


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
    from shared.enums import AnalysisType as _AT

    if not analysis.hints:
        return None
    try:
        h = _json.loads(analysis.hints)
    except Exception:
        return None

    atype = analysis.analysis_type
    if atype == _AT.ARCHIVE_EXTRACT:
        fid = h.get('file_id')
        if fid and fid in hint_file_map:
            return hint_file_map[fid]['path']
        return None
    if atype in (_AT.ARCHIVE_DETECT, _AT.FORMAT_CONVERT, _AT.RISCOS_MODULE_PARSE):
        prefix = h.get('path_prefix', '')
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
    from shared.enums import AnalysisType as _AT

    all_ids = [root.id] + get_all_derived_artefact_ids(root)

    all_artefacts = Artefact.query.filter(Artefact.id.in_(all_ids)).all()
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
                fid = _json.loads(analysis.hints).get('file_id')
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

    # Delete storage files for all derived artefacts (recursively).
    # Must happen before the DB delete so we can still walk the ORM tree.
    for derived in artefact.derived_artefacts:
        delete_artefact_files(derived)

    # Collect all derived artefact IDs (including nested) for bulk deletion.
    all_derived_ids = get_all_derived_artefact_ids(artefact)

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


def delete_artefact_files(artefact):
    """Recursively delete files for an artefact and all its derived artefacts."""
    storage = current_app.storage
    for derived in artefact.derived_artefacts:
        delete_artefact_files(derived)
    try:
        key = get_artefact_storage_key(artefact)
        storage.delete(key)
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
    from shared.storage import S3Storage

    for artefact in item.artefacts:
        # Delete stored files for this artefact and all its derived artefacts.
        delete_artefact_files(artefact)
        if isinstance(storage, S3Storage):
            cleanup_artefact_outputs_s3(artefact, storage)
        else:
            cleanup_artefact_outputs(artefact, current_app.logger)


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


def _collect_item_cleanup_keys(all_artefact_ids):
    """Collect all storage keys and cache prefixes for cleanup.

    Returns a dict with keys: artefact_keys, output_file_keys, output_dir_prefixes,
    cache_prefixes.  All values are storage key strings usable with the storage backend.
    """
    artefact_keys = []
    cache_prefixes = []
    output_dir_prefixes = []
    output_file_keys = []

    if not all_artefact_ids:
        return {
            'artefact_keys': artefact_keys,
            'output_file_keys': output_file_keys,
            'output_dir_prefixes': output_dir_prefixes,
            'cache_prefixes': cache_prefixes,
        }

    # Single query for artefact storage keys and cache prefixes
    rows = db.session.execute(
        select(Artefact.storage_directory, Artefact.storage_path, Artefact.uuid)
        .where(Artefact.id.in_(all_artefact_ids))
    ).all()
    for storage_dir, storage_path, artefact_uuid in rows:
        if storage_path:
            directory = 'outputs' if storage_dir == StorageDirectory.OUTPUTS else 'uploads'
            artefact_keys.append(f"{directory}/{storage_path}")
        cache_prefixes.append(f"outputs/.cache/{artefact_uuid}")

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
        'artefact_keys': artefact_keys,
        'output_file_keys': output_file_keys,
        'output_dir_prefixes': output_dir_prefixes,
        'cache_prefixes': cache_prefixes,
    }


def _background_cleanup_item_files(cleanup, storage, logger_name):
    """Delete item files in a background thread using the storage backend.

    Works with both LocalStorage and S3Storage since all paths are expressed
    as storage keys.  No ORM/app context needed.
    """
    import logging
    logger = logging.getLogger(logger_name)

    for key in cleanup['artefact_keys']:
        try:
            storage.delete(key)
        except Exception as e:
            logger.warning(f"Failed to delete artefact file {key}: {e}")

    for key in cleanup['output_file_keys']:
        try:
            storage.delete(key)
        except Exception as e:
            logger.warning(f"Failed to delete output file {key}: {e}")

    for prefix in cleanup['output_dir_prefixes']:
        try:
            storage.delete_prefix(prefix)
        except Exception as e:
            logger.warning(f"Failed to delete output directory {prefix}: {e}")

    for prefix in cleanup['cache_prefixes']:
        try:
            storage.delete_prefix(prefix)
        except Exception as e:
            logger.warning(f"Failed to delete cache {prefix}: {e}")


def _collect_all_item_ids(item):
    """Collect the item's ID and all descendant item IDs via recursive CTE."""
    base = select(Item.id).where(Item.parent_id == item.id)
    cte = base.cte(name='item_descendants', recursive=True)
    recursive = select(Item.id).where(Item.parent_id == cte.c.id)
    cte = cte.union_all(recursive)
    descendant_ids = [r[0] for r in db.session.execute(select(cte.c.id)).all()]
    return [item.id] + descendant_ids


def bulk_delete_item(item):
    """Delete an item, its descendants, and all related records using bulk SQL.

    Handles item hierarchy (parent/child items) by collecting all descendant
    items first, then bulk-deleting all artefacts across the entire tree.

    Replaces the previous approach of ORM cascade delete which loaded every
    related object into memory and emitted individual DELETEs — too slow for
    items with thousands of artefacts.

    File cleanup runs in a background daemon thread after the DB commit.
    """
    # Phase 1: Collect all item IDs (this item + all descendants)
    all_item_ids = _collect_all_item_ids(item)

    # Phase 2: Collect all artefact IDs via CTE
    all_ids = _collect_item_artefact_ids(all_item_ids)

    # Phase 3: Collect storage keys before deleting DB records
    cleanup = _collect_item_cleanup_keys(all_ids)
    storage = current_app.storage
    logger_name = current_app.logger.name

    # Phase 4: Bulk SQL deletes in FK-safe order
    if all_ids:
        # Null out derived_from_analysis_id before deleting analyses
        Artefact.query.filter(Artefact.id.in_(all_ids)).update(
            {Artefact.derived_from_analysis_id: None}, synchronize_session=False)

        bulk_delete_artefact_dependents(all_ids)
        bulk_delete_artefacts(all_ids)

    # Item-level children (external refs, tags for all items in hierarchy)
    ExternalReference.query.filter(
        ExternalReference.item_id.in_(all_item_ids)
    ).delete(synchronize_session=False)
    db.session.execute(
        item_tags.delete().where(item_tags.c.item_id.in_(all_item_ids)))

    # Break item self-referential FK, then delete all items
    Item.query.filter(Item.id.in_(all_item_ids)).update(
        {Item.parent_id: None}, synchronize_session=False)
    Item.query.filter(Item.id.in_(all_item_ids)).delete(
        synchronize_session=False)

    db.session.commit()

    # Phase 5: Background file cleanup via storage backend (no ORM needed)
    t = threading.Thread(
        target=_background_cleanup_item_files,
        args=(cleanup, storage, logger_name),
        daemon=True,
    )
    t.start()

# vim: ts=4 sw=4 et
