"""Arcology - Hash rescan utility

Provides find_known_file() and the rescan helpers that re-link
ExtractedFile rows to active hash databases without re-analysing.
"""

from ..extensions import db
from ..database import ExtractedFile, Partition, KnownFile, HashDatabase


def find_known_file(md5=None, sha1=None, file_size=None):
    """Return the first active-database KnownFile matching the given hashes.

    Filters to databases with is_active=True so that disabled databases
    are never considered for new or rescanned links.
    """
    if not md5 and not sha1:
        return None
    query = KnownFile.query.join(HashDatabase).filter(HashDatabase.is_active == True)
    if md5:
        query = query.filter(KnownFile.md5 == md5.lower())
    else:
        query = query.filter(KnownFile.sha1 == sha1.lower())
    if file_size is not None:
        query = query.filter(KnownFile.file_size == file_size)
    return query.first()


def rescan_hashes_for_queryset(query, batch_size=500):
    """Re-link hashes for an ExtractedFile queryset.

    Iterates *query* in batches, calling find_known_file() for each
    non-directory file and updating is_known / known_file_id as needed.
    After processing, refreshes the unique_files counter on every
    affected Partition.

    Returns (updated, total) — updated is the number of rows whose
    is_known or known_file_id changed.
    """
    updated = 0
    total = 0
    affected_partition_ids = set()

    # Paginate manually so we don't load the entire table into memory.
    offset = 0
    while True:
        batch = (
            query
            .order_by(ExtractedFile.id)
            .limit(batch_size)
            .offset(offset)
            .all()
        )
        if not batch:
            break

        for ef in batch:
            total += 1
            if ef.is_directory:
                continue

            known = find_known_file(md5=ef.md5, sha1=ef.sha1, file_size=ef.file_size)
            new_id = known.id if known else None
            new_flag = known is not None

            if ef.known_file_id != new_id or ef.is_known != new_flag:
                ef.known_file_id = new_id
                ef.is_known = new_flag
                affected_partition_ids.add(ef.partition_id)
                updated += 1

        db.session.commit()
        offset += len(batch)

    # Refresh unique_files counters for every touched partition.
    for pid in affected_partition_ids:
        partition = db.session.get(Partition, pid)
        if partition:
            partition.unique_files = (
                ExtractedFile.query
                .filter_by(partition_id=pid, is_known=False)
                .count()
            )
    if affected_partition_ids:
        db.session.commit()

    return updated, total


def rescan_hashes_for_artefact(artefact):
    """Rescan all ExtractedFiles belonging to *artefact* (and its partitions).

    Returns (updated, total).
    """
    partition_ids = [p.id for p in artefact.partitions]
    if not partition_ids:
        return 0, 0
    query = ExtractedFile.query.filter(
        ExtractedFile.partition_id.in_(partition_ids)
    )
    return rescan_hashes_for_queryset(query)


def rescan_hashes_all(batch_size=500):
    """Rescan every ExtractedFile in the database.

    Returns (updated, total).
    """
    return rescan_hashes_for_queryset(ExtractedFile.query, batch_size=batch_size)

# vim: ts=4 sw=4 et
