"""Arcology - Hash rescan utility

Provides find_known_file() and the rescan helpers that re-link
ExtractedFile rows to active hash databases without re-analysing.
Also provides queue_product_recognition_for_partitions() to re-trigger
the worker's folder-level product recognition after hash database changes.
"""

import json

from sqlalchemy import or_

from ..extensions import db
from ..database import (
    ExtractedFile, Partition, KnownFile, HashDatabase,
    Analysis, AnalysisType, AnalysisStatus,
    ArtefactRestriction,
)


def _active_known_file_query():
    """Return a KnownFile query limited to active hash databases."""
    return (
        KnownFile.query
        .join(HashDatabase)
        .filter(HashDatabase.is_active == True)
    )


def _filter_known_files_by_hashes(query, *, md5=None, sha1=None):
    """Apply md5/sha1 filters to a KnownFile query."""
    if md5:
        query = query.filter(KnownFile.md5 == md5.lower())
    if sha1:
        query = query.filter(KnownFile.sha1 == sha1.lower())
    return query


def _filter_known_files_by_size(query, file_size=None):
    """Apply an optional file_size filter to a KnownFile query."""
    if file_size is not None:
        query = query.filter(KnownFile.file_size == file_size)
    return query


def _matching_known_files_query(*, md5=None, sha1=None, file_size=None):
    """Return an active KnownFile query for the given hashes and size."""
    return _filter_known_files_by_size(
        _filter_known_files_by_hashes(_active_known_file_query(), md5=md5, sha1=sha1),
        file_size=file_size,
    )


def _match_file_size(candidates, file_size):
    """Filter KnownFile candidates by file_size when both sides provide one."""
    matches = []
    for kf in candidates:
        if file_size is not None and kf.file_size is not None:
            if file_size == kf.file_size:
                matches.append(kf)
        else:
            matches.append(kf)
    return matches


def _dedupe_known_files(matches):
    """Deduplicate KnownFiles by (database_id, product_id)."""
    seen_keys = set()
    deduped = []
    for kf in matches:
        key = (kf.database_id, kf.product_id)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(kf)
    return deduped


def _refresh_partition_unique_counts(partition_ids):
    """Refresh unique_files counters for all touched partitions."""
    for pid in partition_ids:
        partition = db.session.get(Partition, pid)
        if partition:
            partition.unique_files = (
                ExtractedFile.query
                .filter_by(partition_id=pid, is_known=False)
                .count()
            )
    if partition_ids:
        db.session.commit()


def find_known_file(md5=None, sha1=None, file_size=None):
    """Return the best-matching active-database KnownFile for the given hashes.

    Filters to databases with is_active=True so that disabled databases
    are never considered for new or rescanned links.

    When multiple KnownFiles match (same hash appears in more than one
    active database), the one with the lowest KnownFile.id is returned.
    This is deterministic across runs so repeated rescans produce stable
    results: the earliest-inserted entry wins.

    For the deduplication use case (is_known=True/False) the specific
    match returned does not matter; for product attribution it provides
    a stable, predictable answer.
    """
    if not md5 and not sha1:
        return None
    query = _matching_known_files_query(md5=md5, sha1=sha1, file_size=file_size)
    return query.order_by(KnownFile.id).first()


def find_all_known_files_batch(extracted_files):
    """Return all active-database KnownFile matches for a batch of ExtractedFiles.

    Used by the artefact view to show badges for every matching hash
    database, not just the single "primary" match stored in known_file_id.

    Args:
        extracted_files: list of ExtractedFile objects (typically one page)

    Returns:
        dict mapping extracted_file.id -> list of KnownFile objects
        (each with its .database eagerly loaded)
    """
    from sqlalchemy.orm import joinedload

    # Collect md5 values from known files on this page
    ef_by_md5 = {}  # md5 -> list of ExtractedFile
    for ef in extracted_files:
        if ef.is_known and ef.md5:
            ef_by_md5.setdefault(ef.md5.lower(), []).append(ef)

    if not ef_by_md5:
        return {}

    # Single batch query for all matching KnownFiles across active databases
    known_files = (
        _active_known_file_query()
        .filter(KnownFile.md5.in_(list(ef_by_md5.keys())))
        .options(joinedload(KnownFile.database), joinedload(KnownFile.product))
        .order_by(KnownFile.id)
        .all()
    )

    # Build md5 -> list of KnownFile lookup
    kf_by_md5 = {}
    for kf in known_files:
        kf_by_md5.setdefault(kf.md5.lower(), []).append(kf)

    # Map back to extracted_file.id -> list of KnownFile
    result = {}
    for md5, efs in ef_by_md5.items():
        candidates = kf_by_md5.get(md5, [])
        for ef in efs:
            matches = _match_file_size(candidates, ef.file_size)
            deduped = _dedupe_known_files(matches)
            if deduped:
                result[ef.id] = deduped

    return result


def apply_database_restrictions(artefact):
    """Auto-apply download restrictions based on flagged hash databases.

    When an artefact's extracted files match a HashDatabase that has a
    restriction_type set (e.g. MALWARE), this function automatically
    creates ArtefactRestriction records for the artefact.

    Returns the number of newly added restrictions.
    """
    partition_ids = [p.id for p in artefact.partitions]
    if not partition_ids:
        return 0

    # Find all (restriction_type, database_name) pairs from matched databases
    rows = (
        db.session.query(HashDatabase.restriction_type, HashDatabase.name)
        .join(KnownFile, KnownFile.database_id == HashDatabase.id)
        .join(ExtractedFile, ExtractedFile.known_file_id == KnownFile.id)
        .filter(
            ExtractedFile.partition_id.in_(partition_ids),
            ExtractedFile.is_known == True,
            HashDatabase.is_active == True,
            HashDatabase.restriction_type.isnot(None),
        )
        .distinct()
        .all()
    )

    if not rows:
        return 0

    # Group database names by restriction type
    rtype_to_names = {}
    for rtype, name in rows:
        rtype_to_names.setdefault(rtype, []).append(name)

    # Get existing restriction types for this artefact
    existing = {r.restriction_type for r in artefact.restrictions}

    added = 0
    for rtype, names in rtype_to_names.items():
        if rtype not in existing:
            db_list = ', '.join(sorted(names))
            db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=rtype,
                reason=f'Automatically applied: file matches {db_list}',
            ))
            added += 1

    if added:
        db.session.commit()

    return added


def rescan_hashes_for_queryset(query, batch_size=500):
    """Re-link hashes for an ExtractedFile queryset.

    Iterates *query* in batches using cursor-based pagination (ID > last
    seen), which is stable even if the query's filter condition changes as
    rows are updated (e.g. is_known=False flipping to True mid-scan).

    Calls find_known_file() for each non-directory file and updates
    is_known / known_file_id as needed.  After processing, refreshes the
    unique_files counter on every affected Partition.

    Returns (updated, total) — updated is the number of rows whose
    is_known or known_file_id changed.
    """
    updated = 0
    total = 0
    affected_partition_ids = set()
    last_id = 0

    while True:
        batch = (
            query
            .filter(ExtractedFile.id > last_id)
            .order_by(ExtractedFile.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for ef in batch:
            total += 1
            if ef.is_directory:
                last_id = ef.id
                continue

            known = find_known_file(md5=ef.md5, sha1=ef.sha1, file_size=ef.file_size)
            new_id = known.id if known else None
            new_flag = known is not None

            if ef.known_file_id != new_id or ef.is_known != new_flag:
                ef.known_file_id = new_id
                ef.is_known = new_flag
                affected_partition_ids.add(ef.partition_id)
                updated += 1

            last_id = ef.id

        db.session.commit()

    # Refresh unique_files counters for every touched partition.
    _refresh_partition_unique_counts(affected_partition_ids)

    return updated, total


def rescan_hashes_for_artefact(artefact):
    """Rescan all ExtractedFiles belonging to *artefact* (and its partitions).

    After rescanning hashes, also applies any automatic restrictions from
    flagged hash databases (e.g. databases marked as malware).

    Returns (updated, total).
    """
    partition_ids = [p.id for p in artefact.partitions]
    if not partition_ids:
        return 0, 0
    query = ExtractedFile.query.filter(
        ExtractedFile.partition_id.in_(partition_ids)
    )
    result = rescan_hashes_for_queryset(query)

    # Auto-apply restrictions from flagged hash databases
    apply_database_restrictions(artefact)

    return result


def rescan_hashes_all(batch_size=500):
    """Rescan every ExtractedFile in the database.

    Returns (updated, total).
    """
    return rescan_hashes_for_queryset(ExtractedFile.query, batch_size=batch_size)


def rescan_hashes_for_known_file(kf):
    """Targeted rescan: scan only ExtractedFiles whose hashes match *kf*.

    Used after adding or editing a KnownFile so that artefacts are linked
    immediately without a full collection-wide rescan.  Fast because it
    uses the md5/sha1 indexes on extracted_files.

    Returns (updated, total).
    """
    conditions = []
    if kf.md5:
        conditions.append(ExtractedFile.md5 == kf.md5)
    if kf.sha1:
        conditions.append(ExtractedFile.sha1 == kf.sha1)
    if not conditions:
        return 0, 0
    query = ExtractedFile.query.filter(or_(*conditions))
    if kf.file_size is not None:
        query = query.filter(ExtractedFile.file_size == kf.file_size)
    return rescan_hashes_for_queryset(query)


def rescan_links_for_known_file_id(kf_id):
    """Re-evaluate ExtractedFiles that are currently linked to *kf_id*.

    Called when a KnownFile is deleted or its hashes are edited.  Files
    that no longer match will either be re-linked to another active
    KnownFile or have their is_known flag cleared.

    Returns (updated, total).
    """
    query = ExtractedFile.query.filter(ExtractedFile.known_file_id == kf_id)
    return rescan_hashes_for_queryset(query)


def queue_product_recognition_for_partitions(partition_ids):
    """Queue PRODUCT_RECOGNITION analyses for the given partition IDs.

    Called after hash database changes (new files, edits, deletes,
    imports, enable_product_recognition toggled on) so that the worker
    re-runs folder-level product matching against the updated database.

    One Analysis record is created per partition.  To avoid flooding the
    queue, partitions whose artefact already has a PENDING or RUNNING
    PRODUCT_RECOGNITION are skipped (the in-flight analysis will use the
    current database state when it runs).

    Returns the number of newly queued analyses.
    """
    queued = 0
    for pid in partition_ids:
        partition = db.session.get(Partition, pid)
        if not partition:
            continue
        existing = (
            Analysis.query
            .filter_by(
                artefact_id=partition.artefact_id,
                analysis_type=AnalysisType.PRODUCT_RECOGNITION,
            )
            .filter(Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]))
            .first()
        )
        if not existing:
            db.session.add(Analysis(
                artefact_id=partition.artefact_id,
                analysis_type=AnalysisType.PRODUCT_RECOGNITION,
                status=AnalysisStatus.PENDING,
                hints=json.dumps({'partition_uuid': partition.uuid}),
            ))
            queued += 1
    if queued:
        db.session.commit()
    return queued


def rescan_hashes_for_new_known_files(kf_list, batch_size=500):
    """Targeted rescan after a bulk import: scan unlinked files whose
    md5 or sha1 appears in *kf_list*.

    Only considers currently-unlinked files (is_known=False) so the scan
    stays fast for large collections.  Files already linked to other
    databases are not disturbed.

    Returns (updated, total).
    """
    md5_values = [kf.md5 for kf in kf_list if kf.md5]
    sha1_values = [kf.sha1 for kf in kf_list if kf.sha1]
    if not md5_values and not sha1_values:
        return 0, 0

    conditions = []
    if md5_values:
        conditions.append(ExtractedFile.md5.in_(md5_values))
    if sha1_values:
        conditions.append(ExtractedFile.sha1.in_(sha1_values))

    query = ExtractedFile.query.filter(
        ExtractedFile.is_known == False,
        or_(*conditions),
    )
    return rescan_hashes_for_queryset(query, batch_size=batch_size)

# vim: ts=4 sw=4 et
