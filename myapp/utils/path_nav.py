"""Path navigation helpers shared by the File Viewer and the Viewer.

These helpers derive directory-browsing UI state from flat file-path lists.
They are pure-Python and have no Flask or database dependency so they can
be unit-tested in isolation.
"""

from collections.abc import Iterable

from natsort import natsorted, ns


def compute_subdirectories(file_paths: Iterable[str],
                           current_path: str = '') -> list[str]:
    """Return the naturally-sorted immediate subdirectory names beneath
    *current_path*.

    *current_path* is either an empty string (root) or ends with ``'/'``.
    Paths that do not start with *current_path* are skipped.  For each
    matching path, the first component after *current_path* is collected
    (if it is followed by another ``'/'``, indicating the path descends
    into that component).  Results are de-duplicated and sorted
    case-insensitively using natural order.
    """
    subdirs: set[str] = set()
    cp_len = len(current_path)
    for p in file_paths:
        if not p:
            continue
        if current_path:
            if not p.startswith(current_path):
                continue
            relative = p[cp_len:]
        else:
            relative = p
        if '/' in relative:
            first = relative.split('/', 1)[0]
            if first:
                subdirs.add(first)
    return natsorted(subdirs, alg=ns.IGNORECASE)


def split_path_segments(current_path: str) -> list[tuple[str, str]]:
    """Return a list of ``(segment_label, cumulative_path)`` pairs for
    breadcrumb rendering.

    *cumulative_path* always ends with ``'/'``.  Empty or ``None`` input
    returns an empty list.  A trailing slash on the input is tolerated
    and stripped before splitting.
    """
    if not current_path:
        return []
    stripped = current_path.rstrip('/')
    if not stripped:
        return []
    parts = stripped.split('/')
    segments: list[tuple[str, str]] = []
    for i, label in enumerate(parts):
        cumulative = '/'.join(parts[:i + 1]) + '/'
        segments.append((label, cumulative))
    return segments


# vim: ts=4 sw=4 et
