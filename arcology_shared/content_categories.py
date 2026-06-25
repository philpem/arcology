"""Single source of truth for "what follow-on analysis does this file need?".

After a partition or archive is extracted, several follow-on analyses each used
to scan the *whole* file list looking for the files they handle — archives,
convertible Acorn/image files, RISC OS modules, Acorn Replay movies, transcodable
media — and most found nothing.  :func:`classify_content` collapses those five
per-file predicates into one cheap, metadata-only function so the categories
present in an extraction can be computed in a single pass and the matching
follow-on analyses dispatched only when their content actually exists.

It is deliberately a *candidate* filter, exactly like the predicates it
replaces: it looks only at the filename and RISC OS filetype, never at file
contents.  The handlers still confirm a genuine match (ARMovie magic, ffprobe,
archive parsing) before doing real work — classification only decides whether a
handler is worth queueing at all.
"""

import enum
import os
from .archive_formats import get_archive_by_extension, get_archive_by_filetype
from .artefact_types import (
    MEDIA_EXTENSIONS,
    is_replay_file,
    media_kind_for_riscos_filetype,
    viewable_artefact_type,
)

# RISC OS filetype &FFA — a relocatable Module (RISCOS_MODULE_PARSE).
_RISCOS_MODULE_FILETYPE = 'ffa'


class ContentCategory(enum.Enum):
    """A kind of extracted file that triggers a specific follow-on analysis.

    The mapping from category to the :class:`AnalysisType` it dispatches lives in
    the worker's follow-up dispatcher; this enum is just the vocabulary the
    classifier and that dispatcher share.
    """

    ARCHIVE = 'archive'              # → ARCHIVE_EXTRACT (nested archive)
    CONVERTIBLE = 'convertible'      # → FORMAT_CONVERT (Sprite/Draw/Text/image)
    RISCOS_MODULE = 'riscos_module'  # → RISCOS_MODULE_PARSE (filetype &FFA)
    REPLAY = 'replay'                # → REPLAY_PROCESS (Acorn Replay / ARMovie)
    MEDIA = 'media'                  # → MEDIA_TRANSCODE (audio/video)


def classify_content(filename: str,
                     risc_os_filetype: str | None) -> set[ContentCategory]:
    """Return the set of follow-on content categories a file belongs to.

    A file can match more than one category (the categories are not mutually
    exclusive), so the result is a set.  ``filename`` is the stored name and
    ``risc_os_filetype`` the lowercase RISC OS filetype hex (or None) — the same
    pair every follow-on handler keys its own selection off.
    """
    name = filename or ''
    ft = (risc_os_filetype or '').lower()
    categories: set[ContentCategory] = set()

    # ARCHIVE — RISC OS filetype first, then extension (mirrors ARCHIVE_DETECT).
    if (get_archive_by_filetype(ft) if ft else None) or get_archive_by_extension(name):
        categories.add(ContentCategory.ARCHIVE)

    # CONVERTIBLE — Sprite/DrawFile/Text/image the converter can render.
    if viewable_artefact_type(name, ft) is not None:
        categories.add(ContentCategory.CONVERTIBLE)

    # RISCOS_MODULE — relocatable module (filetype &FFA).
    if ft == _RISCOS_MODULE_FILETYPE:
        categories.add(ContentCategory.RISCOS_MODULE)

    # REPLAY — Acorn Replay / ARMovie movie.
    if is_replay_file(name, ft):
        categories.add(ContentCategory.REPLAY)

    # MEDIA — transcodable audio/video, by extension or RISC OS media filetype.
    _, ext = os.path.splitext(name.lower())
    if ext in MEDIA_EXTENSIONS or media_kind_for_riscos_filetype(ft) is not None:
        categories.add(ContentCategory.MEDIA)

    return categories

# vim: ts=4 sw=4 et
