"""Path navigation helpers shared by the File Viewer and the Viewer.

These helpers derive directory-browsing UI state from flat file-path lists.
They are pure-Python and have no Flask or database dependency so they can
be unit-tested in isolation.
"""

from collections import defaultdict
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


def _extract_dir_set(paths: Iterable[str]) -> set[str]:
    """Return every unique implied directory path (always ending with '/').

    For a path 'a/b/c.txt', the implied directories are 'a/' and 'a/b/'.
    """
    dirs: set[str] = set()
    for path in paths:
        parts = path.split('/')
        for depth in range(1, len(parts)):
            dirs.add('/'.join(parts[:depth]) + '/')
    return dirs


def _build_children_map(all_dirs: frozenset[str]) -> dict[str, list[str]]:
    """Build a {parent_path → [child_paths]} map from a flat directory set.

    Single O(N) pass.  Each directory is registered under its immediate parent
    so that tree traversal can use O(1) dict lookups instead of rescanning the
    full set at every level (which was O(N²) for wide flat trees).
    """
    children_map: dict[str, list[str]] = {}
    for d in all_dirs:
        # Strip trailing slash, then split off the last component to get parent.
        # "a/b/c/" → stripped="a/b/c" → parent="a/b/"
        # "toplevel/" → stripped="toplevel" → no '/' → parent=""
        stripped = d.rstrip('/')
        parent = (stripped.rsplit('/', 1)[0] + '/') if '/' in stripped else ''
        children_map.setdefault(parent, []).append(d)
    return children_map


def _build_from_map(children_map: dict[str, list[str]], prefix: str,
                    archive_paths: set[str]) -> list[dict]:
    """Recursively build tree nodes from a pre-computed children map."""
    result = []
    for dir_path in natsorted(children_map.get(prefix, []), alg=ns.IGNORECASE):
        name = dir_path[len(prefix):].rstrip('/')
        if not name:
            continue
        result.append({
            'name': name,
            'path': dir_path,
            'is_archive': dir_path.rstrip('/') in archive_paths,
            'children': _build_from_map(children_map, dir_path, archive_paths),
        })
    return result


def build_directory_tree(
    path_rows: Iterable[tuple[str, str]],
    partitions: list,
    archive_paths: set[str] | None = None,
) -> list[dict]:
    """Build a partition-rooted directory tree for the artefact tree panel.

    *path_rows* is an iterable of ``(path, partition_uuid)`` tuples — one row
    per ``ExtractedFile``.  *partitions* is the ordered list of ``Partition``
    ORM objects for the artefact (and its derived artefacts).

    Returns a list of partition-level dicts::

        [{"partition": <Partition>, "children": [<node>, ...]}, ...]

    Each directory node is::

        {"name": str, "path": str (ends with '/'), "is_archive": bool, "children": [...]}

    Partitions with no files are omitted.  The partition order from *partitions*
    is preserved.
    """
    _arc: set[str] = archive_paths or set()

    by_partition: dict[str, list[str]] = defaultdict(list)
    for path, p_uuid in path_rows:
        by_partition[p_uuid].append(path)

    result = []
    for partition in partitions:
        paths = by_partition.get(partition.uuid, [])
        if not paths:
            continue
        all_dirs = frozenset(_extract_dir_set(paths))
        children_map = _build_children_map(all_dirs)
        result.append({
            'partition': partition,
            'children': _build_from_map(children_map, '', _arc),
        })
    return result


# vim: ts=4 sw=4 et
