"""Product recognition matcher and bounded backfill step.

This module is the **single** implementation of HashDB product recognition.
Both recognition paths call into it server-side:

  * upload-time, per-partition (worker triggers ``/partitions/<uuid>/recognise-step``)
  * HashDB-wide backfill (worker triggers ``/hash-databases/<id>/recognition-step``)

Recognition is pure database work — read ``extracted_files`` / ``known_files``,
match folders in memory, write ``recognised_products`` — so it lives next to the
data rather than being shipped over the API to the worker.  The worker drives a
bounded **step loop**: each call processes one capped batch of products and
returns a cursor, so no single web request runs long (see ``recognise_products_step``).

Matching rules (preserved from the pre-refactor duplicated implementations):
  * all *required* files must match;
  * *optional* files are counted but not mandatory;
  * an *optional-only* product needs at least one optional match;
  * when ``path_match_enabled`` is set, the file's path relative to the matched
    folder is compared (the folder prefix is stripped first);
  * hash comparison uses the **best hash available** per known file —
    SHA-256, then SHA-1, then MD5 (issue #620).
"""

from datetime import datetime, timezone
from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import selectinload
from ..database import (
    ExtractedFile,
    HashDatabase,
    KnownProduct,
    RecognisedProduct,
)
from ..extensions import db


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

    table = RecognisedProduct.__table__
    dialect = db.session.get_bind().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(table).values(rows).on_conflict_do_nothing(
            index_elements=['partition_id', 'product_id', 'folder_path'],
        )
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        stmt = insert(table).values(rows).on_conflict_do_nothing(
            index_elements=['partition_id', 'product_id', 'folder_path'],
        )
    else:
        stmt = table.insert().values(rows)
    db.session.execute(stmt)


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


def recognise_products_step(*, database_id=None, partition_id=None,
                            last_product_id=0, limit=25):
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

    Returns ``{'done', 'processed', 'matches', 'next_product_id'}``.
    """
    query = KnownProduct.query.options(selectinload(KnownProduct.known_files))
    if database_id is not None:
        query = query.filter(KnownProduct.database_id == database_id)
    else:
        # All recognition-enabled databases (per-partition path).
        query = query.join(
            HashDatabase, HashDatabase.id == KnownProduct.database_id
        ).filter(HashDatabase.enable_product_recognition.is_(True))

    products = (
        query
        .filter(KnownProduct.id > last_product_id)
        .order_by(KnownProduct.id)
        .limit(limit)
        .all()
    )
    if not products:
        return {'done': True, 'processed': 0, 'matches': 0,
                'next_product_id': last_product_id}

    product_dicts = {p.id: _product_to_dict(p) for p in products}
    product_ids = list(product_dicts.keys())

    # Replace any prior rows for this product batch (scoped to the partition
    # when running the per-partition path).
    delete_q = RecognisedProduct.query.filter(
        RecognisedProduct.product_id.in_(product_ids))
    if partition_id is not None:
        delete_q = delete_q.filter(RecognisedProduct.partition_id == partition_id)
    delete_q.delete(synchronize_session=False)

    # Index each product's *candidate* files (required, or all when there are
    # no required files) by their best hash, so a folder only verifies products
    # that share at least one matching file hash.
    hash_sets = {'md5': set(), 'sha1': set(), 'sha256': set()}
    product_ids_by_hash = {'md5': {}, 'sha1': {}, 'sha256': {}}
    for pid, pdict in product_dicts.items():
        candidates = pdict['required_files'] or pdict['optional_files']
        for entry in candidates:
            kind, value = select_best_hash(
                entry['md5'], entry['sha1'], entry['sha256'])
            if not value:
                continue
            hash_sets[kind].add(value)
            product_ids_by_hash[kind].setdefault(value, set()).add(pid)

    # Find candidate folders: any folder holding a file whose hash matches a
    # candidate hash of some product in this batch.
    candidate_folders = set()
    folder_product_ids = {}
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
            folder = path.rsplit('/', 1)[0] if path and '/' in path else ''
            folder_key = (part_id, folder)
            candidate_folders.add(folder_key)
            candidates = folder_product_ids.setdefault(folder_key, set())
            for kind, value in (('md5', f_md5), ('sha1', f_sha1), ('sha256', f_sha256)):
                if value:
                    candidates.update(
                        product_ids_by_hash[kind].get(value.lower(), set()))

    # Fetch the immediate files of every candidate folder and build per-folder
    # hash indexes for exact verification.
    folders_by_partition = {}
    for part_id, folder in candidate_folders:
        folders_by_partition.setdefault(part_id, set()).add(folder)

    folder_index = {}
    if folders_by_partition:
        folder_conditions = [
            _folder_file_condition(part_id, folder)
            for part_id, folders in folders_by_partition.items()
            for folder in folders
        ]
        file_rows = (
            ExtractedFile.query
            .filter(
                ExtractedFile.is_directory == False,  # noqa: E712
                or_(*folder_conditions),
            )
            .all()
        )
        for f in file_rows:
            path = f.path or ''
            folder = path.rsplit('/', 1)[0] if '/' in path else ''
            if folder not in folders_by_partition.get(f.partition_id, set()):
                continue
            rel = path.rsplit('/', 1)[1] if '/' in path else path
            idx = folder_index.setdefault(
                (f.partition_id, folder),
                {'md5s': set(), 'sha1s': set(), 'sha256s': set(), 'path_map': {}},
            )
            md5 = (f.md5 or '').lower()
            sha1 = (f.sha1 or '').lower()
            sha256 = (f.sha256 or '').lower()
            if not (md5 or sha1 or sha256):
                continue
            if md5:
                idx['md5s'].add(md5)
            if sha1:
                idx['sha1s'].add(sha1)
            if sha256:
                idx['sha256s'].add(sha256)
            idx['path_map'][rel.lower()] = {'md5': md5, 'sha1': sha1, 'sha256': sha256}

    # Verify candidate (folder, product) pairs and collect rows.
    matches = 0
    rows_to_insert = []
    for (part_id, folder), idx in folder_index.items():
        for product_id in folder_product_ids.get((part_id, folder), set()):
            verified = verify_product_in_folder(product_dicts[product_id], idx, folder)
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
