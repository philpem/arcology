"""Content-addressed storage paths for deduplicated media transcodes.

Shared by the **worker** (which writes transcode outputs) and the **web app**
(which looks them up for the cache endpoint, reverse-resolves owners for the
serving gate, and reclaims them in GC).  The path scheme is security- and
GC-relevant on both sides, so it lives in exactly one place here rather than
being re-derived independently in each component.
"""

# Leading segment of every content-addressed transcode output.  Distinguishes a
# shared, refcounted output from the per-artefact ``outputs/{item}/{artefact}/``
# trees, so the per-artefact ``delete_prefix`` GC sweep never touches it.
CONTENT_ADDRESSED_MEDIA_PREFIX = 'media/'

# Bump to invalidate every cached transcode output (e.g. when the ffmpeg flags
# or codec choices change).  Part of the content-addressed output path, so a new
# value simply routes transcodes to a fresh namespace; old outputs are left for
# the storage GC to reclaim once nothing references them.  Defined here (rather
# than in the worker) so the worker (writer) and the web app (which dedups and
# reclaims these outputs) derive the identical path from one source of truth.
MEDIA_TRANSCODE_TOOL_VERSION = '1'


def transcode_output_subdir(sha256: str, tool_version: str) -> str:
    """``outputs/`` subdir for a transcode, keyed on the SOURCE file's SHA-256.

    Keyed on the source hash (deterministic) and the transcoder version, so two
    artefacts holding byte-identical media share one stored output.
    """
    return f'{CONTENT_ADDRESSED_MEDIA_PREFIX}{sha256}/{tool_version}'


def transcode_movie_name(ext: str) -> str:
    """Canonical leaf filename for the transcoded media stream."""
    return f'movie.{ext}'


def transcode_poster_name(suffix: str) -> str:
    """Canonical leaf filename for a transcode poster (``suffix`` includes dot)."""
    return f'poster{suffix}'


def transcode_poster_like(subdir: str) -> str:
    """SQL ``LIKE`` pattern matching the (single) poster in a transcode subdir.

    A content-addressed dir holds at most one poster, stored as ``poster.<ext>``.
    """
    return f'{subdir}/poster.%'

# vim: ts=4 sw=4 et
