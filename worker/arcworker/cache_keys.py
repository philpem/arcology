"""
Shared storage-key builders for the worker's partition-image cache.

PARTITION_DETECT decompresses whole-disc images once and caches them under
``outputs/.cache/<artefact-uuid>/`` (both on local disk and in S3) so that the
follow-on FILE_EXTRACTION / ARMLOCK_REMOVE jobs do not have to decompress
again.  Several modules build or reconstruct that key by hand:

* :mod:`worker.arcworker.analyses.partition` writes the cache,
* :meth:`AnalysisWorker._resolve_partition_image` reconstructs it as a fallback
  when a downstream job runs on a different worker, and
* the CLEANUP job deletes the whole per-artefact prefix.

Keeping the ``.cache/<uuid>/...`` convention in one place means those callers
cannot silently disagree about the layout.
"""

# Top-level directory (relative to the outputs root / storage 'outputs' dir)
# under which decompressed partition images are cached.
CACHE_DIR_NAME = '.cache'


def artefact_cache_prefix(artefact_uuid: str) -> str:
    """Return the cache key prefix for one artefact's cached images.

    e.g. ``".cache/<uuid>"``.  Used as a path relative to the outputs root and
    as a storage-key prefix (CLEANUP deletes everything under it).
    """
    return f"{CACHE_DIR_NAME}/{artefact_uuid}"


def partition_cache_relpath(artefact_uuid: str, name: str) -> str:
    """Return the cache key for a single cached file, relative to outputs.

    e.g. ``".cache/<uuid>/partition_0.img"``.  Pass the result to
    ``storage.storage_key('outputs', ...)`` to obtain a backend key.
    """
    return f"{artefact_cache_prefix(artefact_uuid)}/{name}"


def partition_image_filename(index: int) -> str:
    """Return the cached-image filename for a partition index.

    e.g. ``"partition_0.img"``.
    """
    return f"partition_{index}.img"

# vim: ts=4 sw=4 et
