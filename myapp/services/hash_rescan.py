"""Arcology - Hash rescan utility

Provides find_known_file() and the rescan helpers that re-link
ExtractedFile rows to active hash databases without re-analysing.
Also queues partition-level recognition for newly processed artefacts and
database-level recognition backfills after HashDB content changes.
"""

import json
from sqlalchemy import and_, case, func, or_
from ..database import (
    Analysis,
    AnalysisStatus,
    AnalysisType,
    ArtefactRestriction,
    ExtractedFile,
    ExtractedFileRestriction,
    HashDatabase,
    KnownFile,
    KnownProduct,
    Partition,
    ProductRecognitionStatus,
    RecognisedProduct,
)
from ..extensions import db


def _active_known_file_query():
    """Return a KnownFile query limited to active hash databases."""
    return (
        KnownFile.query
        .join(HashDatabase)
        .filter(HashDatabase.is_active == True)
    )


def _filter_known_files(query, *, md5=None, sha1=None, file_size=None):
    """Apply optional md5/sha1/size filters to a KnownFile query.

    Size matching is lenient: a KnownFile with no recorded size (NULL)
    matches any file size, since NULL means "size unknown" rather than
    "size zero".  Only KnownFiles whose recorded size differs from the
    given size are excluded.  This matches the in-memory predicates used
    by _match_file_size() and _find_known_files_batch() so that the live
    upload-time linking path and the rescan path agree on what counts as
    a match.
    """
    if md5:
        query = query.filter(KnownFile.md5 == md5.lower())
    if sha1:
        query = query.filter(KnownFile.sha1 == sha1.lower())
    if file_size is not None:
        query = query.filter(
            or_(KnownFile.file_size.is_(None), KnownFile.file_size == file_size)
        )
    return query


def _matching_known_files_query(*, md5=None, sha1=None, file_size=None):
    """Return an active KnownFile query for the given hashes and size."""
    return _filter_known_files(_active_known_file_query(), md5=md5, sha1=sha1, file_size=file_size)


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
    """Recompute the denormalised file counters for all touched partitions.

    Both ``unique_files`` (non-directory files not matched to a known file) and
    ``total_files`` (every extracted-file row) are derived here from the actual
    rows in a single aggregate query, so they self-heal from any incremental
    drift the way ``unique_files`` always has.
    """
    if not partition_ids:
        return

    pid_list = list(partition_ids)

    # Single query per partition: total row count plus the unknown-non-directory
    # subset, using a conditional sum so both counters come from one scan.
    unique_case = case(
        (
            and_(
                ExtractedFile.known_file_id.is_(None),
                ExtractedFile.is_directory == False,
            ),
            1,
        ),
        else_=0,
    )
    rows = (
        db.session.query(
            ExtractedFile.partition_id,
            func.count(ExtractedFile.id),
            func.coalesce(func.sum(unique_case), 0),
        )
        .filter(ExtractedFile.partition_id.in_(pid_list))
        .group_by(ExtractedFile.partition_id)
        .all()
    )
    total_map = {pid: total for pid, total, _ in rows}
    unique_map = {pid: unique for pid, _, unique in rows}

    # Update all affected partitions (including those with zero files).
    partitions = Partition.query.filter(Partition.id.in_(pid_list)).all()
    for partition in partitions:
        partition.total_files = total_map.get(partition.id, 0)
        partition.unique_files = unique_map.get(partition.id, 0)

    db.session.commit()


def find_known_file(md5=None, sha1=None, file_size=None):
    """Return the best-matching active-database KnownFile for the given hashes.

    Filters to databases with is_active=True so that disabled databases
    are never considered for new or rescanned links.

    When multiple KnownFiles match (same hash appears in more than one
    active database), the one with the lowest KnownFile.id is returned.
    This is deterministic across runs so repeated rescans produce stable
    results: the earliest-inserted entry wins.

    For the deduplication use case (linked vs unlinked) the specific
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

    # Find all (restriction_type, database_name, extracted_file_id) tuples from matched databases
    rows = (
        db.session.query(HashDatabase.restriction_type, HashDatabase.name, ExtractedFile.id)
        .join(KnownFile, KnownFile.database_id == HashDatabase.id)
        .join(ExtractedFile, ExtractedFile.known_file_id == KnownFile.id)
        .filter(
            ExtractedFile.partition_id.in_(partition_ids),
            ExtractedFile.known_file_id.isnot(None),
            HashDatabase.is_active == True,
            HashDatabase.restriction_type.isnot(None),
        )
        .all()
    )

    if not rows:
        return 0

    # Group database names and file IDs by restriction type
    rtype_to_names = {}
    rtype_to_ef_ids = {}
    for rtype, name, ef_id in rows:
        rtype_to_names.setdefault(rtype, set()).add(name)
        rtype_to_ef_ids.setdefault(rtype, set()).add(ef_id)

    # Apply per-file restrictions to each matching ExtractedFile
    for rtype, ef_ids in rtype_to_ef_ids.items():
        db_list = ', '.join(sorted(rtype_to_names[rtype]))
        already_restricted = {
            r.extracted_file_id
            for r in ExtractedFileRestriction.query.filter(
                ExtractedFileRestriction.extracted_file_id.in_(ef_ids),
                ExtractedFileRestriction.restriction_type == rtype,
            ).all()
        }
        for ef_id in ef_ids:
            if ef_id not in already_restricted:
                db.session.add(ExtractedFileRestriction(
                    extracted_file_id=ef_id,
                    restriction_type=rtype,
                    reason=f'Automatically applied: file matches {db_list}',
                ))

    # Get existing artefact-level restriction types
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

    if added or rtype_to_ef_ids:
        db.session.commit()

    return added


def _find_known_files_batch(extracted_files):
    """Batch-fetch the best KnownFile match for a list of ExtractedFiles.

    Returns a dict mapping extracted_file.id -> KnownFile (or absent if no
    match).  Uses a single query to fetch all candidate KnownFiles from
    active databases, then matches in-memory using the same logic as
    find_known_file(): md5/sha1 must match, and size matches leniently
    (a KnownFile with no recorded size matches any file size).

    This replaces calling find_known_file() per file (N+1 queries) with a
    single bulk query per batch.
    """
    # Collect unique hash values from this batch
    md5s = set()
    sha1s = set()
    files_to_match = []
    for ef in extracted_files:
        if ef.is_directory:
            continue
        files_to_match.append(ef)
        if ef.md5:
            md5s.add(ef.md5.lower())
        if ef.sha1:
            sha1s.add(ef.sha1.lower())

    if not md5s and not sha1s:
        return {}

    # Single query: fetch all candidate KnownFiles from active databases
    conditions = []
    if md5s:
        conditions.append(KnownFile.md5.in_(list(md5s)))
    if sha1s:
        conditions.append(KnownFile.sha1.in_(list(sha1s)))

    candidates = (
        _active_known_file_query()
        .filter(or_(*conditions))
        .order_by(KnownFile.id)
        .all()
    )

    # Build lookup indexes
    by_md5 = {}
    by_sha1 = {}
    for kf in candidates:
        if kf.md5:
            by_md5.setdefault(kf.md5.lower(), []).append(kf)
        if kf.sha1:
            by_sha1.setdefault(kf.sha1.lower(), []).append(kf)

    # Match each file using the same logic as find_known_file: md5/sha1
    # must match, and size matches leniently (a candidate with no recorded
    # size matches any file size).
    result = {}
    for ef in files_to_match:
        pool = None
        if ef.md5:
            pool = set(by_md5.get(ef.md5.lower(), []))
        if ef.sha1:
            sha1_set = set(by_sha1.get(ef.sha1.lower(), []))
            pool = pool & sha1_set if pool is not None else sha1_set
        if pool is None:
            continue
        # file_size filter: keep candidates where either side is None or sizes match
        if ef.file_size is not None:
            pool = {kf for kf in pool
                    if kf.file_size is None or kf.file_size == ef.file_size}
        if pool:
            result[ef.id] = min(pool, key=lambda kf: kf.id)

    return result


def find_known_files_for_records(records):
    """Batch-match raw file records to active-database KnownFiles.

    Mirrors find_known_file() semantics (md5/sha1 must match, size matches
    leniently) but resolves a whole list of records in a single query instead
    of one query per file.  Used by the worker file-registration endpoint to
    avoid an N+1 query pattern when a partition contains thousands of files.

    Args:
        records: list of mappings, each exposing 'md5', 'sha1' and 'file_size'
            keys (e.g. the file dicts POSTed to /partitions/<uuid>/files).

    Returns:
        list aligned with *records*; each element is the best-matching
        KnownFile (lowest id, same tie-break as find_known_file()) or None.
    """
    md5s = set()
    sha1s = set()
    for rec in records:
        md5 = rec.get('md5')
        sha1 = rec.get('sha1')
        if md5:
            md5s.add(md5.lower())
        if sha1:
            sha1s.add(sha1.lower())

    if not md5s and not sha1s:
        return [None] * len(records)

    conditions = []
    if md5s:
        conditions.append(KnownFile.md5.in_(list(md5s)))
    if sha1s:
        conditions.append(KnownFile.sha1.in_(list(sha1s)))

    candidates = (
        _active_known_file_query()
        .filter(or_(*conditions))
        .order_by(KnownFile.id)
        .all()
    )

    by_md5 = {}
    by_sha1 = {}
    for kf in candidates:
        if kf.md5:
            by_md5.setdefault(kf.md5.lower(), []).append(kf)
        if kf.sha1:
            by_sha1.setdefault(kf.sha1.lower(), []).append(kf)

    results = []
    for rec in records:
        md5 = rec.get('md5')
        sha1 = rec.get('sha1')
        if not md5 and not sha1:
            results.append(None)
            continue
        pool = None
        if md5:
            pool = set(by_md5.get(md5.lower(), []))
        if sha1:
            sha1_set = set(by_sha1.get(sha1.lower(), []))
            pool = pool & sha1_set if pool is not None else sha1_set
        if not pool:
            results.append(None)
            continue
        file_size = rec.get('file_size')
        if file_size is not None:
            pool = {kf for kf in pool
                    if kf.file_size is None or kf.file_size == file_size}
        results.append(min(pool, key=lambda kf: kf.id) if pool else None)

    return results


def rescan_hashes_for_queryset(query, batch_size=500, *, progress=None,
                               progress_every=50000):
    """Re-link hashes for an ExtractedFile queryset.

    Iterates *query* in batches using cursor-based pagination (ID > last
    seen), which is stable even if the query's filter condition changes as
    rows are updated (e.g. an unlinked file gaining a known_file_id mid-scan).

    Uses _find_known_files_batch() to match each batch with a single DB
    query instead of one query per file.  After processing, refreshes the
    unique_files counter on every affected Partition.

    ``progress`` is an optional ``callable(str)`` for status lines (the CLI
    passes ``click.echo``); when ``None`` the rescan is silent and no row
    count is taken.  A progress line is emitted roughly every
    ``progress_every`` files scanned.

    Returns (updated, total) — updated is the number of rows whose
    known_file_id changed.
    """
    updated = 0
    total = 0
    affected_partition_ids = set()
    last_id = 0

    # Only pay for the upfront COUNT(*) when a caller actually wants progress.
    total_estimate = query.count() if progress is not None else None
    if progress is not None:
        progress(f"  {total_estimate} file(s) to scan …")
    last_report = 0

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

        matches = _find_known_files_batch(batch)

        for ef in batch:
            total += 1
            last_id = ef.id
            if ef.is_directory:
                continue

            known = matches.get(ef.id)
            new_id = known.id if known else None

            if ef.known_file_id != new_id:
                ef.known_file_id = new_id
                affected_partition_ids.add(ef.partition_id)
                updated += 1

        db.session.commit()

        if progress is not None and total - last_report >= progress_every:
            last_report = total
            pct = f" ({total * 100 // total_estimate}%)" if total_estimate else ""
            progress(f"  {total}/{total_estimate} scanned{pct}, {updated} relinked")

    # Refresh unique_files counters for every touched partition.
    if progress is not None and affected_partition_ids:
        progress(f"  refreshing counters for {len(affected_partition_ids)} partition(s) …")
    _refresh_partition_unique_counts(affected_partition_ids)

    return updated, total


def rescan_hashes_for_artefact(artefact, *, progress=None):
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
    result = rescan_hashes_for_queryset(query, progress=progress)

    # Auto-apply restrictions from flagged hash databases
    apply_database_restrictions(artefact)

    return result


def rescan_hashes_all(batch_size=500, *, progress=None):
    """Rescan every ExtractedFile in the database.

    Returns (updated, total).
    """
    return rescan_hashes_for_queryset(
        ExtractedFile.query, batch_size=batch_size, progress=progress)


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
    KnownFile or have their known_file_id cleared.

    Returns (updated, total).
    """
    query = ExtractedFile.query.filter(ExtractedFile.known_file_id == kf_id)
    return rescan_hashes_for_queryset(query)


def queue_product_recognition_for_partitions(partition_ids):
    """Queue PRODUCT_RECOGNITION analyses for the given partition IDs.

    Called after artefact extraction or hash rescans so that the task runner
    re-runs folder-level product matching for newly processed partitions.

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


def _queue_system_analysis_once(
        analysis_type,
        hints,
        statuses=(AnalysisStatus.PENDING, AnalysisStatus.RUNNING),
        *,
        commit=True):
    """Queue a system Analysis job once for a stable hints payload.

    A new job is suppressed only when an existing one already sits in one of
    *statuses*.  The default dedups against PENDING or RUNNING jobs; recognition
    backfills pass ``statuses=(PENDING,)`` so a content change mid-run can queue
    a fresh follow-up even while an earlier backfill is still RUNNING.
    """
    hints_json = json.dumps(hints, sort_keys=True)
    existing = (
        Analysis.query
        .filter_by(
            artefact_id=None,
            analysis_type=analysis_type,
            hints=hints_json,
        )
        .filter(Analysis.status.in_(list(statuses)))
        .first()
    )
    if existing:
        return existing, False

    analysis = Analysis(
        artefact_id=None,
        analysis_type=analysis_type,
        status=AnalysisStatus.PENDING,
        hints=hints_json,
    )
    db.session.add(analysis)
    if commit:
        db.session.commit()
    return analysis, True


def queue_hashdb_link_job(database_id):
    """Queue a relink job for one HashDB (run in-process by the task runner)."""
    return _queue_system_analysis_once(
        AnalysisType.HASHDB_LINK,
        {'database_id': database_id},
    )


def queue_hashdb_delete_job(database_id):
    """Queue a background delete (reap) job for one HashDB.

    The web delete route marks the database is_deleting=True / is_active=False
    and queues this; the task runner drains its rows in bounded batches via
    ``delete_one_step`` (myapp/services/hashdb_jobs.py), in-process with direct
    DB access, so no web request runs long.
    """
    return _queue_system_analysis_once(
        AnalysisType.HASHDB_DELETE,
        {'database_id': database_id},
    )


def queue_hashdb_recognition_job(database_id, *, commit=True):
    """Queue a product-recognition backfill for one HashDB (task-runner job)."""
    return _queue_system_analysis_once(
        AnalysisType.HASHDB_RECOGNITION,
        {'database_id': database_id},
        statuses=(AnalysisStatus.PENDING,),
        commit=commit,
    )


def has_pending_recognition_job(database_id):
    """True if a PENDING HASHDB_RECOGNITION backfill is queued for this HashDB.

    A backfill claimed by the task runner is RUNNING, so a PENDING row is always
    a genuine follow-up requested after the current run started — meaning the
    current run's results are already stale and must not be reported COMPLETED.
    """
    hints_json = json.dumps({'database_id': database_id}, sort_keys=True)
    return (
        Analysis.query
        .filter_by(
            artefact_id=None,
            analysis_type=AnalysisType.HASHDB_RECOGNITION,
            hints=hints_json,
            status=AnalysisStatus.PENDING,
        )
        .first()
    ) is not None


def mark_hashdb_recognition_pending(database):
    """Mark a HashDB's product-recognition counts stale and waiting for backfill."""
    database.product_recognition_status = ProductRecognitionStatus.PENDING
    database.product_recognition_error = None
    database.product_recognition_updated_at = None


def clear_hashdb_recognition(database):
    """Remove recognised-product rows and status for a HashDB."""
    product_id_query = (
        db.session.query(KnownProduct.id)
        .filter(KnownProduct.database_id == database.id)
    )
    RecognisedProduct.query.filter(
        RecognisedProduct.product_id.in_(product_id_query)
    ).delete(synchronize_session=False)
    database.product_recognition_status = None
    database.product_recognition_updated_at = None
    database.product_recognition_error = None


def queue_hashdb_recognition_backfill(database):
    """Persist a pending status and queue one HashDB recognition backfill."""
    mark_hashdb_recognition_pending(database)
    analysis, queued = queue_hashdb_recognition_job(database.id, commit=False)
    db.session.commit()
    return analysis, queued


def rescan_hashes_for_new_known_files(kf_list, batch_size=500):
    """Targeted rescan after a bulk import: scan unlinked files whose
    md5 or sha1 appears in *kf_list*.

    Only considers currently-unlinked files (known_file_id IS NULL) so the scan
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
        ExtractedFile.known_file_id.is_(None),
        or_(*conditions),
    )
    return rescan_hashes_for_queryset(query, batch_size=batch_size)


def link_new_known_files(database, new_kf_list):
    """Link existing extracted files to freshly-imported KnownFiles, and queue
    a HashDB recognition backfill when the database enables product recognition.

    Shared by the web import route and the REST API bulk-add endpoint so that
    both entry points link the collection after an import.  Without this, an
    imported database shows zero matches until a manual rescan is triggered.
    """
    if not database.is_active or not new_kf_list:
        return

    rescan_hashes_for_new_known_files(new_kf_list)

    if not database.enable_product_recognition:
        return

    queue_hashdb_recognition_backfill(database)

# vim: ts=4 sw=4 et
