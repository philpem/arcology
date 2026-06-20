"""Run-to-completion drivers for the DB-only HashDB maintenance jobs.

Historically the ``HASH_RESCAN`` / ``PRODUCT_RECOGNITION`` /
``HASHDB_LINK`` / ``HASHDB_DELETE`` / ``HASHDB_RECOGNITION`` analyses were
*driven* by the analysis worker looping bounded HTTP "step" endpoints in
``myapp/blueprints/api.py`` — but every byte of work and every DB write already
happened in the web process.  The taskrunner (``myapp/taskrunner``) now runs
these jobs in-process with direct DB access, so it needs the same logic without
the per-request wall-clock deadline / statement-timeout machinery that existed
only to keep an HTTP request short.

This module holds:

  * the shared delete state machine and its helpers
    (``delete_one_step`` / ``delete_chunk_with_retry`` / ``finalise_hashdb_delete``)
    and the recognition-status finaliser (``finalise_recognition_status``).
    The bounded API step endpoints are thin wrappers over these so the two
    consumers cannot drift;
  * ``run_*_job`` wrappers the taskrunner calls, each accepting ``heartbeat``
    and ``check_cancelled`` callbacks so a long job stays alive (bumps
    ``progress_updated_at``) and aborts promptly when cancelled.

All ``run_*_job`` functions own their own commits and return a small summary
dict; they raise ``JobCancelled`` if ``check_cancelled`` signals.
"""

import time
from datetime import datetime, timezone
from sqlalchemy.exc import OperationalError
from ..database import (
    ExtractedFile,
    HashDatabase,
    KnownFile,
    KnownProduct,
    ProductRecognitionStatus,
    RecognisedProduct,
)
from ..extensions import db
from ..utils.db_helpers import is_deadlock
from .hash_rescan import (
    has_pending_recognition_job,
    queue_hashdb_link_job,
    queue_hashdb_recognition_backfill,
    queue_product_recognition_for_partitions,
    rescan_hashes_for_artefact,
    rescan_hashes_for_new_known_files,
)
from .recognition import recognise_products_step


class JobCancelled(Exception):
    """Raised by a run_*_job when its check_cancelled callback signals a cancel."""


def _noop_heartbeat(**kwargs):
    pass


def _noop_check_cancelled():
    pass


# =============================================================================
# Shared delete state machine (also used by the bounded API delete-step endpoint)
# =============================================================================

# Per-statement chunk sizes for the bounded delete step.  Small enough that any
# single statement stays well under the worker read timeout even on a database
# matching a large slice of the collection; the step loops over chunks until a
# wall-clock deadline (when one is given), so throughput is set by the deadline,
# not these.
_DELETE_UNLINK_CHUNK = 5000      # extracted_files unlinked per statement
_DELETE_RECOGNISED_CHUNK = 5000  # recognised_products deleted per statement
_DELETE_KNOWN_FILE_CHUNK = 5000  # known_files deleted per statement


def delete_chunk_with_retry(fn):
    """Run one bounded delete statement, retrying briefly on deadlock.

    The reap step still races a relink/recognition step that may hold locks on
    the same rows in the opposite order.  PostgreSQL aborts one side; the small
    chunk size means the other side releases promptly, so a short retry succeeds
    rather than failing the whole step.  Returns the rowcount.
    """
    attempts = 4
    for attempt in range(attempts):
        try:
            count = fn()
            db.session.commit()
            return count
        except OperationalError as exc:
            db.session.rollback()
            if not is_deadlock(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def finalise_hashdb_delete(db_id):
    """Delete the HashDatabase row and queue relinks against other active DBs.

    Returns the number of other active databases for which a HASHDB_LINK job
    was newly queued.  The freed extracted_files now have known_file_id=NULL, so
    each other database's bounded relink picks up any that match it.
    """
    # Deadlock-retry the final row delete too — it races a relink the same way
    # the chunk deletes do; the helper commits and retries on deadlock.
    delete_chunk_with_retry(
        lambda: db.session.delete(db.session.get(HashDatabase, db_id)))

    relinked = 0
    other_active = (
        HashDatabase.query
        .filter(HashDatabase.is_active.is_(True), HashDatabase.id != db_id)
        .all()
    )
    for other in other_active:
        _, queued = queue_hashdb_link_job(other.id)
        if queued:
            relinked += 1
    return relinked


def delete_one_step(database, cursor, deadline=None):
    """Run one bounded reap step for a soft-deleted HashDB.

    A stateless state machine derived from the current row counts, run in
    FK-safe phase order:

      A. unlink extracted_files still pointing at this DB's known_files;
      B/C. delete recognised_products (by product) and known_files;
      D. (final) delete known_products, delete the hash_databases row, and queue
         HASHDB_LINK for every other active DB so freed files re-match.

    ``deadline`` is an optional ``time.monotonic()`` value: a soft wall-clock
    budget after which the step yields with ``done=False`` and an advanced
    cursor (the bounded API endpoint passes one to keep its HTTP request short).
    When ``deadline`` is ``None`` the step runs the whole reap to completion in
    one call (the taskrunner path).

    Returns ``{'done', 'cursor', 'deleted'[, 'relinked_databases'], 'progress_label'}``.
    ``cursor = incoming + rows_touched`` strictly advances until the final step.
    """
    db_id = database.id
    kf_id_query = db.session.query(KnownFile.id).filter(KnownFile.database_id == db_id)
    product_id_query = db.session.query(KnownProduct.id).filter(
        KnownProduct.database_id == db_id)
    progress_label = f"Deleting HashDB '{database.name}'"
    touched = 0

    while True:
        # Always do at least one unit of work per call before yielding on the
        # deadline.  Checking the deadline at the top would let a non-positive
        # budget return cursor+1/done=False with zero work done — forever, since
        # the advancing cursor hides the stall.  Only break once we've made
        # progress.  When deadline is None we never break here (run to done).
        if touched and deadline is not None and time.monotonic() >= deadline:
            break

        # Phase A: unlink extracted_files linked to this DB (until none remain).
        ef_ids = (
            db.session.query(ExtractedFile.id)
            .filter(ExtractedFile.known_file_id.in_(kf_id_query))
            .order_by(ExtractedFile.id)
            .limit(_DELETE_UNLINK_CHUNK)
        )
        n = delete_chunk_with_retry(lambda ids=ef_ids: (
            ExtractedFile.query
            .filter(ExtractedFile.id.in_(ids))
            .update({'known_file_id': None}, synchronize_session=False)
        ))
        if n:
            touched += n
            continue

        # Phase B: delete recognised_products for this DB's products.
        rp_ids = (
            db.session.query(RecognisedProduct.id)
            .filter(RecognisedProduct.product_id.in_(product_id_query))
            .order_by(RecognisedProduct.id)
            .limit(_DELETE_RECOGNISED_CHUNK)
        )
        n = delete_chunk_with_retry(lambda ids=rp_ids: (
            RecognisedProduct.query
            .filter(RecognisedProduct.id.in_(ids))
            .delete(synchronize_session=False)
        ))
        if n:
            touched += n
            continue

        # Phase C: delete known_files for this DB.
        kf_ids = (
            db.session.query(KnownFile.id)
            .filter(KnownFile.database_id == db_id)
            .order_by(KnownFile.id)
            .limit(_DELETE_KNOWN_FILE_CHUNK)
        )
        n = delete_chunk_with_retry(lambda ids=kf_ids: (
            KnownFile.query
            .filter(KnownFile.id.in_(ids))
            .delete(synchronize_session=False)
        ))
        if n:
            touched += n
            continue

        # Phase D (final): no child rows remain — drop known_products and the
        # database row, then queue relinks of the freed files against other
        # active databases.
        delete_chunk_with_retry(lambda: (
            KnownProduct.query.filter(KnownProduct.database_id == db_id)
            .delete(synchronize_session=False)
        ))
        relinked = finalise_hashdb_delete(db_id)
        return {
            'done': True,
            'cursor': cursor + touched,
            'deleted': touched,
            'relinked_databases': relinked,
            'progress_label': progress_label,
        }

    # Deadline reached after making progress this call: report it and continue.
    return {
        'done': False,
        'cursor': cursor + touched,
        'deleted': touched,
        'progress_label': progress_label,
    }


def finalise_recognition_status(database):
    """Mark a finished backfill COMPLETED unless a fresh follow-up is queued.

    A PENDING HASHDB_RECOGNITION for the same database means a content change
    landed after this run started, so its counts are already stale.  Leave the
    status at PENDING in that case so the view does not surface stale results as
    authoritative; the queued follow-up will refresh and complete them.
    """
    if has_pending_recognition_job(database.id):
        database.product_recognition_status = ProductRecognitionStatus.PENDING
        database.product_recognition_updated_at = None
    else:
        database.product_recognition_status = ProductRecognitionStatus.COMPLETED
        database.product_recognition_updated_at = datetime.now(timezone.utc)


# =============================================================================
# Run-to-completion job drivers (taskrunner)
# =============================================================================

def run_hash_rescan_job(artefact, *, heartbeat=_noop_heartbeat,
                        check_cancelled=_noop_check_cancelled):
    """Re-link one artefact's extracted files and queue any product recognition.

    Mirrors the old ``/artefact/<uuid>/hash-rescan`` endpoint, in-process.
    """
    check_cancelled()
    updated, total = rescan_hashes_for_artefact(artefact)
    heartbeat(current=updated, total=total,
              label=f"Re-linking files for '{artefact.label}'")

    recognition_queued = 0
    has_recognition = HashDatabase.query.filter_by(
        is_active=True, enable_product_recognition=True).first()
    if has_recognition:
        partition_ids = [p.id for p in artefact.partitions if p.total_files > 0]
        if partition_ids:
            recognition_queued = queue_product_recognition_for_partitions(partition_ids)

    parts = [f'{updated}/{total} files linked']
    if recognition_queued:
        parts.append(f'{recognition_queued} product recognition job(s) queued')
    return {
        'summary': ', '.join(parts),
        'updated': updated,
        'total': total,
        'recognition_queued': recognition_queued,
    }


def run_hashdb_link_job(db_id, *, heartbeat=_noop_heartbeat,
                        check_cancelled=_noop_check_cancelled):
    """Re-link every KnownFile in one HashDB against the extracted-file corpus.

    Loops a KnownFile cursor over ``rescan_hashes_for_new_known_files`` to
    completion (no per-request deadline), heartbeating between pages.
    """
    database = db.session.get(HashDatabase, db_id)
    if database is None:
        return {'summary': 'database no longer exists', 'updated': 0, 'processed': 0}

    label = f"Linking files in HashDB '{database.name}'"
    page = 500       # known files fetched per page
    chunk_size = 50  # extracted-file relink batch
    last_id = 0
    processed = 0
    updated = 0
    scanned = 0
    while True:
        check_cancelled()
        batch = (
            KnownFile.query
            .filter(KnownFile.database_id == db_id, KnownFile.id > last_id)
            .order_by(KnownFile.id)
            .limit(page)
            .all()
        )
        if not batch:
            break
        for i in range(0, len(batch), chunk_size):
            chunk = batch[i:i + chunk_size]
            u, t = rescan_hashes_for_new_known_files(chunk)
            updated += u
            scanned += t
            processed += len(chunk)
            last_id = chunk[-1].id
        heartbeat(current=processed, total=database.file_count or 0, label=label)

    recognition_queued = False
    if database.enable_product_recognition:
        _, recognition_queued = queue_hashdb_recognition_backfill(database)

    parts = [f'{processed} known files checked', f'{updated}/{scanned} extracted files linked']
    if recognition_queued:
        parts.append('product recognition backfill queued')
    return {
        'summary': ', '.join(parts),
        'processed': processed,
        'updated': updated,
        'scanned': scanned,
        'recognition_queued': bool(recognition_queued),
    }


def run_hashdb_delete_job(db_id, *, heartbeat=_noop_heartbeat,
                          check_cancelled=_noop_check_cancelled):
    """Reap a soft-deleted HashDB to completion via the shared delete machine.

    Drives ``delete_one_step`` with a modest soft deadline so it yields between
    batches for heartbeat / cancellation, in-process and without HTTP.
    """
    database = db.session.get(HashDatabase, db_id)
    if database is None:
        return {'summary': 'database already deleted', 'rows_deleted': 0,
                'relinked_databases': 0}

    label = f"Deleting HashDB '{database.name}'"
    cursor = 0
    relinked = 0
    while True:
        check_cancelled()
        # A short yield budget keeps the heartbeat cadence tight on a huge reap;
        # the loop continues until the state machine reports done.
        result = delete_one_step(database, cursor, deadline=time.monotonic() + 30)
        cursor = result['cursor']
        heartbeat(current=cursor, total=None, label=label)
        if result['done']:
            relinked = result.get('relinked_databases', 0)
            break

    parts = [f'{cursor} row(s) deleted']
    if relinked:
        parts.append(f're-linking freed files against {relinked} other database(s)')
    return {
        'summary': ', '.join(parts),
        'rows_deleted': cursor,
        'relinked_databases': relinked,
    }


def _run_recognition_loop(*, database_id=None, partition_id=None, label,
                          heartbeat, check_cancelled):
    """Loop ``recognise_products_step`` to completion (no per-step deadline).

    Returns ``(processed, matches)``.  Commits per step.  With ``deadline=None``
    the step never reports ``timed_out``, so a slow product just runs through —
    acceptable for the taskrunner (no gthread worker to protect, direct DB).
    """
    last_id = 0
    processed = 0
    matches = 0
    while True:
        check_cancelled()
        result = recognise_products_step(
            database_id=database_id,
            partition_id=partition_id,
            last_product_id=last_id,
            limit=25,
            deadline=None,
        )
        processed += result.get('processed', 0)
        matches += result.get('matches', 0)
        last_id = result.get('next_product_id', last_id)
        db.session.commit()
        heartbeat(current=processed, total=None, label=label)
        if result.get('done'):
            break
    return processed, matches


def run_hashdb_recognition_job(db_id, *, heartbeat=_noop_heartbeat,
                               check_cancelled=_noop_check_cancelled):
    """Backfill product recognition for one HashDB to completion."""
    database = db.session.get(HashDatabase, db_id)
    if database is None:
        return {'summary': 'database no longer exists', 'processed': 0, 'matches': 0}

    if not database.enable_product_recognition:
        database.product_recognition_status = None
        database.product_recognition_updated_at = None
        database.product_recognition_error = None
        db.session.commit()
        return {'summary': 'product recognition disabled', 'processed': 0, 'matches': 0}

    database.product_recognition_status = ProductRecognitionStatus.RUNNING
    database.product_recognition_error = None
    db.session.commit()

    label = f"Recognising products in HashDB '{database.name}'"
    try:
        processed, matches = _run_recognition_loop(
            database_id=db_id, label=label,
            heartbeat=heartbeat, check_cancelled=check_cancelled)
    except JobCancelled:
        database.product_recognition_status = ProductRecognitionStatus.PENDING
        database.product_recognition_updated_at = None
        db.session.commit()
        raise
    except Exception as exc:
        db.session.rollback()
        database.product_recognition_status = ProductRecognitionStatus.FAILED
        database.product_recognition_error = str(exc)[:1000]
        database.product_recognition_updated_at = datetime.now(timezone.utc)
        db.session.commit()
        raise

    finalise_recognition_status(database)
    db.session.commit()
    return {
        'summary': f'{processed} product(s) checked; {matches} recognition match(es) found',
        'processed': processed,
        'matches': matches,
    }


def run_partition_recognition_job(partition, *, heartbeat=_noop_heartbeat,
                                  check_cancelled=_noop_check_cancelled):
    """Match every recognition-enabled HashDB's products against one partition."""
    label = f"Recognising products in partition {partition.uuid}"
    processed, matches = _run_recognition_loop(
        partition_id=partition.id, label=label,
        heartbeat=heartbeat, check_cancelled=check_cancelled)
    return {
        'summary': f'{processed} product(s) checked; {matches} recognition match(es) found',
        'processed': processed,
        'matches': matches,
    }

# vim: ts=4 sw=4 et
