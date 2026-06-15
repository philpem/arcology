"""Shared RISC OS module / Acorn Replay metadata lookups for file listings.

Both the artefact file listing (`_file_listing.html`) and the global file
search results surface a module (`bi-cpu`) and an Acorn Replay / ARMovie
(`bi-film`) viewer icon next to matching files.  The underlying
``RiscosModule`` / ``ReplayMovie`` queries live here so the two views stay
consistent; only the dict key differs:

* :func:`metadata_by_path` keys by ``file_path`` for a *single* artefact's
  derived tree, where path is a stable identity (and matching loosely by
  path lets modules inside nested extractions show on the parent).
* :func:`metadata_by_file_id` keys by ``ExtractedFile.id`` for *cross-artefact*
  search rows, where path is not unique across unrelated artefacts.
"""

from myapp.database import ReplayMovie, RiscosModule


def _modules(artefact_ids):
    if not artefact_ids:
        return []
    return RiscosModule.query.filter(
        RiscosModule.artefact_id.in_(artefact_ids)
    ).all()


def _movies(artefact_ids):
    if not artefact_ids:
        return []
    return ReplayMovie.query.filter(
        ReplayMovie.artefact_id.in_(artefact_ids)
    ).all()


def metadata_by_path(artefact_ids):
    """Return ``(module_info, replay_info)`` keyed by ``file_path``.

    For the artefact file listing: *artefact_ids* is the viewed artefact's
    whole derived tree, so a path is a unique key within that one context.
    """
    module_info = {m.file_path: m for m in _modules(artefact_ids) if m.file_path}
    replay_info = {v.file_path: v for v in _movies(artefact_ids) if v.file_path}
    return module_info, replay_info


def metadata_by_file_id(file_rows):
    """Return ``(module_info, replay_info)`` keyed by ``ExtractedFile.id``.

    For the file search results: *file_rows* is an iterable of
    ``(ExtractedFile, partition, Artefact, item)`` tuples whose rows may belong
    to unrelated artefacts, so we match on ``(artefact_id, path)`` and key the
    result by file id (path alone is not unique across artefacts).
    """
    module_info = {}
    replay_info = {}
    if not file_rows:
        return module_info, replay_info

    # (artefact_id, path) → ExtractedFile.id, for the visible result rows only.
    path_to_ef = {}
    artefact_ids = set()
    for ef, _part, art, _item in file_rows:
        path_to_ef[(art.id, ef.path)] = ef.id
        artefact_ids.add(art.id)

    for m in _modules(artefact_ids):
        ef_id = path_to_ef.get((m.artefact_id, m.file_path))
        if ef_id is not None:
            module_info[ef_id] = m

    for v in _movies(artefact_ids):
        ef_id = path_to_ef.get((v.artefact_id, v.file_path))
        if ef_id is not None:
            replay_info[ef_id] = v

    return module_info, replay_info


# vim: ts=4 sw=4 et
