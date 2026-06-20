"""Optional fuzzy hashing (TLSH) for byte-level similarity of leaf blobs.

This complements the content-set similarity in ``myapp/services/similarity.py``:
where that compares the *set* of files an artefact contains, TLSH compares the
raw bytes of a single file/blob, answering "which one file changed between two
otherwise-identical discs?" and handling monolithic artefacts that have no
extractable file set.

TLSH (``py-tlsh``) is an **optional** C++ extension.  It is installed in the
worker and web Docker images but is intentionally *not* in ``requirements.txt``,
so CI and minimal installs run without it.  When unavailable, ``compute_tlsh()``
returns ``None`` and every TLSH feature degrades gracefully.
"""

from pathlib import Path

try:
    import tlsh as _tlsh
    HAS_TLSH = True
except Exception:  # pragma: no cover - exercised only where the lib is absent
    _tlsh = None
    HAS_TLSH = False

# TLSH needs at least this many bytes (with some variance) to produce a digest.
TLSH_MIN_BYTES = 50

# Conventional similarity threshold: a diff at or below this is "similar".
TLSH_SIMILAR_DISTANCE = 80


def _clean(digest) -> str | None:
    if not digest or digest == "TNULL":
        return None
    return digest


def compute_tlsh(data) -> str | None:
    """Return the TLSH digest of ``data`` (bytes or a file path), or ``None``.

    File paths are streamed so large blobs do not have to be read into memory.
    ``None`` is returned when the library is unavailable, the input is too small
    or too low-variance for TLSH, or hashing otherwise fails.
    """
    if not HAS_TLSH:
        return None
    if isinstance(data, (str, Path)):
        try:
            with open(data, "rb") as fh:
                return compute_tlsh_stream(fh)
        except OSError:
            return None
    try:
        if data is None or len(data) < TLSH_MIN_BYTES:
            return None
        return _clean(_tlsh.hash(data))
    except Exception:
        return None


def compute_tlsh_stream(fileobj, chunk_size=65536) -> str | None:
    """TLSH digest of a binary file object, read incrementally. ``None`` on failure."""
    if not HAS_TLSH:
        return None
    try:
        h = _tlsh.Tlsh()
        total = 0
        for chunk in iter(lambda: fileobj.read(chunk_size), b""):
            h.update(chunk)
            total += len(chunk)
        if total < TLSH_MIN_BYTES:
            return None
        h.final()
        if not h.isValid():
            return None
        return _clean(h.hexdigest())
    except Exception:
        return None


def tlsh_diff(a: str, b: str) -> int | None:
    """TLSH distance between two digests (0 = identical; larger = more different).

    Returns ``None`` if the library is unavailable or either digest is missing.
    """
    if not HAS_TLSH or not a or not b:
        return None
    try:
        return _tlsh.diff(a, b)
    except Exception:
        return None

# vim: ts=4 sw=4 et
