"""Product recognition matcher and bounded backfill step.

This module is the **single** implementation of HashDB product recognition.
Both recognition paths call into it directly:

  * upload-time, per-partition (``run_partition_recognition_job``)
  * HashDB-wide backfill (``run_hashdb_recognition_job``)

both in ``myapp/services/hashdb_jobs.py``, run in-process by the task runner.

Recognition is pure database work — read ``extracted_files`` / ``known_files``,
match folders in memory, write ``recognised_products`` — so it runs next to the
data with direct DB access.  ``recognise_products_step`` processes one capped
batch of products and returns a cursor; the task runner loops it to completion.
(The ``deadline`` argument is a soft wall-clock budget retained for callers that
want bounded steps; the task runner passes ``deadline=None`` and runs to done.)

Matching rules:
  * a product is only matchable if it has at least one *mandatory* (required)
    file — a product with no required file has no discriminating fingerprint and
    is ignored by the matcher (the web UI flags such products so a curator can
    add a mandatory file);
  * all *required* files must match;
  * *optional* files add confidence and are counted, but never gate a match;
  * when ``path_match_enabled`` is set, the file's path relative to the matched
    folder is compared (the folder prefix is stripped first);
  * hash comparison uses the **best hash available** per known file —
    SHA-256, then SHA-1, then MD5 (issue #620).
"""

import time
from datetime import datetime, timezone
from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import selectinload
from ..database import (
    ExtractedFile,
    HashDatabase,
    KnownProduct,
    RecognisedProduct,
)
from ..utils.db_helpers import insert_ignore_conflict

# Maximum number of per-folder LIKE conditions OR'd into a single verification
# query.  Bounds statement size (and keeps the planner on the (partition_id,
# path) index) when a product batch resolves to many candidate folders.
_FOLDER_QUERY_BATCH = 100


def select_best_hash(md5, sha1, sha256):
    """Return ``(kind, value)`` for the best available hash, or ``(None, None)``.

    Preference order is SHA-256, then SHA-1, then MD5 (issue #620).  Values are
    lower-cased so comparisons are case-insensitive.
    """
    for kind, value in (('sha256', sha256), ('sha1', sha1), ('md5', md5)):
        if value:
            return kind, value.lower()
    return None, None


def _file_matches(known, folder_index, path_match_enabled, folder_prefix):
    """True if *known* (a dict with md5/sha1/sha256/relative_path) is present in
    the folder described by *folder_index*, using best-hash comparison."""
    kind, value = select_best_hash(
        known.get('md5'), known.get('sha1'), known.get('sha256'))
    if not value:
        return False

    if path_match_enabled and known.get('relative_path'):
        rel_path = known['relative_path'].lower()
        if folder_prefix and rel_path.startswith(folder_prefix):
            rel_path = rel_path[len(folder_prefix):]
        entry = folder_index['path_map'].get(rel_path)
        return bool(entry and entry.get(kind) == value)

    return value in folder_index[kind + 's']


def verify_product_in_folder(product, folder_index, folder_path):
    """Check one product against one folder index.

    *product* is a dict with ``path_match_enabled``, ``required_files`` and
    ``optional_files`` (lists of dicts with md5/sha1/sha256/relative_path).
    *folder_index* has ``md5s``/``sha1s``/``sha256s`` sets and a ``path_map``
    of ``rel_path_lower -> {'md5','sha1','sha256'}``.

    Returns ``(required_matched, required_total, optional_matched,
    optional_total)`` on a match, or ``None``.
    """
    required = product['required_files']
    optional = product['optional_files']
    if not required and not optional:
        return None

    path_match_enabled = product.get('path_match_enabled', False)
    folder_lower = folder_path.lower()
    folder_prefix = folder_lower + '/' if folder_lower else ''

    required_matched = sum(
        1 for kf in required
        if _file_matches(kf, folder_index, path_match_enabled, folder_prefix)
    )
    if required and required_matched < len(required):
        return None

    optional_matched = sum(
        1 for kf in optional
        if _file_matches(kf, folder_index, path_match_enabled, folder_prefix)
    )
    if not required and optional_matched == 0:
        return None

    return required_matched, len(required), optional_matched, len(optional)


# ---------------------------------------------------------------------------
# Persistence helpers (moved here from blueprints/api.py)
# ---------------------------------------------------------------------------

def insert_recognised_product_rows(rows):
    """Insert recognition rows, ignoring duplicates from overlapping workers.

    The unique key is ``(partition_id, product_id, folder_path)`` — concurrent
    backfill and per-partition runs may race to insert the same row, so we
    rely on ``ON CONFLICT DO NOTHING`` rather than a prior existence check.
    """
    if not rows:
        return

    now = datetime.now(timezone.utc)
    for row in rows:
        row.setdefault('created_at', now)

    insert_ignore_conflict(
        RecognisedProduct, rows, ('partition_id', 'product_id', 'folder_path'))


def _sql_like_escape(value):
    return (
        value
        .replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )


def _folder_file_condition(partition_id, folder):
    """A clause selecting the *immediate* files of one folder in one partition.

    Matches files whose path begins with ``folder/`` but contains no further
    ``/`` (i.e. direct children, not files in sub-folders).  The empty folder
    means top-level files (no ``/`` at all).
    """
    if folder:
        folder_prefix = _sql_like_escape(folder) + '/'
        return and_(
            ExtractedFile.partition_id == partition_id,
            ExtractedFile.path.like(folder_prefix + '%', escape='\\'),
            not_(ExtractedFile.path.like(folder_prefix + '%/%', escape='\\')),
        )
    return and_(
        ExtractedFile.partition_id == partition_id,
        not_(ExtractedFile.path.like('%/%', escape='\\')),
    )


# ---------------------------------------------------------------------------
# Bounded step — the one entry point both endpoints call
# ---------------------------------------------------------------------------

def _product_to_dict(product):
    required = []
    optional = []
    for kf in product.known_files:
        entry = {
            'md5': kf.md5,
            'sha1': kf.sha1,
            'sha256': kf.sha256,
            'relative_path': kf.relative_path,
        }
        (required if kf.is_required else optional).append(entry)
    return {
        'product_id': product.id,
        'path_match_enabled': product.path_match_enabled,
        'required_files': required,
        'optional_files': optional,
    }


def _recognition_products_query(database_id, last_product_id, limit, *,
                                with_files=False):
    """The cursor-paged KnownProduct query both step paths use.

    ``database_id`` scopes to one HashDB (backfill); ``None`` selects products of
    every recognition-enabled database (the per-partition path).  ``partition_id``
    only scopes the extracted-file side, never the product selection, so it is
    not a parameter here.
    """
    query = KnownProduct.query
    if with_files:
        query = query.options(selectinload(KnownProduct.known_files))
    if database_id is not None:
        query = query.filter(KnownProduct.database_id == database_id)
    else:
        query = query.join(
            HashDatabase, HashDatabase.id == KnownProduct.database_id
        ).filter(HashDatabase.enable_product_recognition.is_(True))
    return (
        query
        .filter(KnownProduct.id > last_product_id)
        .order_by(KnownProduct.id)
        .limit(limit)
    )


def recognition_batch_last_id(database_id=None, last_product_id=0, limit=25):
    """Return the last product id of the batch a step *would* process, or None.

    Used by the endpoints to compute a skip cursor when a step is aborted by the
    PostgreSQL statement timeout (which raises before the step can report its
    own ``next_product_id``)."""
    rows = (
        _recognition_products_query(database_id, last_product_id, limit)
        .with_entities(KnownProduct.id)
        .all()
    )
    return rows[-1][0] if rows else None


def recognise_products_step(*, database_id=None, partition_id=None,
                            last_product_id=0, limit=25, deadline=None):
    """Run one bounded recognition step and persist the resulting rows.

    Exactly one scope is used:

      * ``database_id`` — HashDB backfill: match this DB's products across **all**
        partitions.
      * ``partition_id`` — per-partition (upload-time): match the products of
        **all** recognition-enabled DBs against this one partition.

    Products are paged by ``KnownProduct.id > last_product_id`` (capped at
    *limit*), so each call is bounded regardless of collection size.  Existing
    recognised rows for the product batch (scoped to the partition when given)
    are replaced, preserving the "re-run replaces stale matches" semantics.

    Does **not** commit — the caller owns the transaction.

    *deadline* is an optional ``time.monotonic()`` value: a soft wall-clock
    budget so a step that runs many sub-statements (each individually under the
    statement timeout) cannot collectively overrun the worker's read timeout.
    When it is exceeded mid-step the function abandons the batch and returns
    ``{'timed_out': True, ...}`` (the caller rolls back and the worker retries a
    smaller batch).  The per-statement PostgreSQL ``statement_timeout`` set by
    the endpoint still guards a single runaway query.

    Returns ``{'done', 'processed', 'matches', 'next_product_id'}`` — or
    ``{'timed_out': True, 'done': False, 'next_product_id': last_product_id}``
    when the deadline was hit.
    """
    products = (
        _recognition_products_query(database_id, last_product_id, limit,
                                    with_files=True)
        .all()
    )
    if not products:
        return {'done': True, 'processed': 0, 'matches': 0,
                'next_product_id': last_product_id}

    def _timed_out():
        # Report the attempted batch's last product id as the cursor: at the
        # worker's minimum batch size (one product) this lets the caller skip a
        # single un-processable product and continue, rather than failing the
        # whole backfill.  While the batch is larger the caller ignores this and
        # retries the same cursor with a smaller batch.
        return {'timed_out': True, 'done': False, 'processed': 0, 'matches': 0,
                'next_product_id': products[-1].id}

    product_dicts = {p.id: _product_to_dict(p) for p in products}
    product_ids = list(product_dicts.keys())

    # Replace any prior rows for this product batch (scoped to the partition
    # when running the per-partition path).
    delete_q = RecognisedProduct.query.filter(
        RecognisedProduct.product_id.in_(product_ids))
    if partition_id is not None:
        delete_q = delete_q.filter(RecognisedProduct.partition_id == partition_id)
    delete_q.delete(synchronize_session=False)

    # Only products with mandatory (required) files can be recognised.  A
    # product with no required file has no discriminating fingerprint, so the
    # matcher ignores it entirely — otherwise a single ubiquitous shared file (a
    # common !Boot or !System module) would make such a product "match" thousands
    # of unrelated folders.  The web UI flags these products so a curator can add
    # a mandatory file to make them matchable.
    matchable = {pid: pd for pid, pd in product_dicts.items() if pd['required_files']}
    if not matchable:
        return {
            'done': len(products) < limit,
            'processed': len(products),
            'matches': 0,
            'next_product_id': products[-1].id,
        }

    # Seed candidate folders on each product's MANDATORY hashes (the
    # discriminating files), so a product is only considered in folders that
    # already contain a required file of it.
    required_pids_by_hash = {'md5': {}, 'sha1': {}, 'sha256': {}}
    hash_sets = {'md5': set(), 'sha1': set(), 'sha256': set()}
    for pid, pdict in matchable.items():
        for entry in pdict['required_files']:
            kind, value = select_best_hash(
                entry['md5'], entry['sha1'], entry['sha256'])
            if not value:
                continue
            hash_sets[kind].add(value)
            required_pids_by_hash[kind].setdefault(value, set()).add(pid)

    # One query finds every folder holding a product's mandatory file, with the
    # candidate (folder -> products) map.  Each matched file is an immediate
    # child of its folder (folder = parent of path).
    candidate_folders = {}  # (part_id, folder) -> set(product_id)
    if any(hash_sets.values()):
        conditions = []
        if hash_sets['md5']:
            conditions.append(ExtractedFile.md5.in_(hash_sets['md5']))
        if hash_sets['sha1']:
            conditions.append(ExtractedFile.sha1.in_(hash_sets['sha1']))
        if hash_sets['sha256']:
            conditions.append(ExtractedFile.sha256.in_(hash_sets['sha256']))

        rows_q = (
            ExtractedFile.query
            .with_entities(
                ExtractedFile.partition_id, ExtractedFile.path,
                ExtractedFile.md5, ExtractedFile.sha1, ExtractedFile.sha256)
            .filter(ExtractedFile.is_directory == False, or_(*conditions))  # noqa: E712
        )
        if partition_id is not None:
            rows_q = rows_q.filter(ExtractedFile.partition_id == partition_id)

        for part_id, path, f_md5, f_sha1, f_sha256 in rows_q.all():
            if not path:
                continue
            md5 = (f_md5 or '').lower()
            sha1 = (f_sha1 or '').lower()
            sha256 = (f_sha256 or '').lower()
            folder, _, _rel = path.rpartition('/')  # '' folder for a top-level file
            pids = candidate_folders.setdefault((part_id, folder), set())
            for kind, value in (('md5', md5), ('sha1', sha1), ('sha256', sha256)):
                if value:
                    pids.update(required_pids_by_hash[kind].get(value, set()))

    if deadline is not None and time.monotonic() > deadline:
        return _timed_out()

    # Fetch the immediate files of just those candidate folders (few, because
    # mandatories are discriminating) and verify each candidate product fully:
    # confirm its whole required set, then count optionals.
    matches = 0
    rows_to_insert = []
    folders_by_partition = {}
    for part_id, folder in candidate_folders:
        folders_by_partition.setdefault(part_id, set()).add(folder)

    if folders_by_partition:
        folder_conditions = [
            _folder_file_condition(part_id, folder)
            for part_id, folders in folders_by_partition.items()
            for folder in folders
        ]
        full_index = {}
        for i in range(0, len(folder_conditions), _FOLDER_QUERY_BATCH):
            if deadline is not None and time.monotonic() > deadline:
                return _timed_out()
            chunk = folder_conditions[i:i + _FOLDER_QUERY_BATCH]
            file_rows = (
                ExtractedFile.query
                .with_entities(
                    ExtractedFile.partition_id, ExtractedFile.path,
                    ExtractedFile.md5, ExtractedFile.sha1, ExtractedFile.sha256)
                .filter(ExtractedFile.is_directory == False, or_(*chunk))  # noqa: E712
                .all()
            )
            for part_id, path, f_md5, f_sha1, f_sha256 in file_rows:
                if not path:
                    continue
                folder, _, rel = path.rpartition('/')
                if folder not in folders_by_partition.get(part_id, ()):
                    continue
                md5 = (f_md5 or '').lower()
                sha1 = (f_sha1 or '').lower()
                sha256 = (f_sha256 or '').lower()
                if not (md5 or sha1 or sha256):
                    continue
                idx = full_index.setdefault(
                    (part_id, folder),
                    {'md5s': set(), 'sha1s': set(), 'sha256s': set(), 'path_map': {}})
                if md5:
                    idx['md5s'].add(md5)
                if sha1:
                    idx['sha1s'].add(sha1)
                if sha256:
                    idx['sha256s'].add(sha256)
                idx['path_map'][rel.lower()] = {'md5': md5, 'sha1': sha1, 'sha256': sha256}

        for (part_id, folder), idx in full_index.items():
            for product_id in candidate_folders.get((part_id, folder), ()):
                verified = verify_product_in_folder(matchable[product_id], idx, folder)
                if verified is None:
                    continue
                required_matched, required_total, optional_matched, optional_total = verified
                rows_to_insert.append({
                    'partition_id': part_id,
                    'product_id': product_id,
                    'folder_path': folder if folder else '/',
                    'required_matched': required_matched,
                    'required_total': required_total,
                    'optional_matched': optional_matched,
                    'optional_total': optional_total,
                })
                matches += 1

    insert_recognised_product_rows(rows_to_insert)

    # A short batch means the cursor reached the end; a full batch may have
    # more, so the worker makes one extra call that hits the empty branch above.
    return {
        'done': len(products) < limit,
        'processed': len(products),
        'matches': matches,
        'next_product_id': products[-1].id,
    }

# vim: ts=4 sw=4 et
