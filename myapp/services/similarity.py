"""Content-set similarity between artefacts.

Two artefacts are "similar" when they contain substantially the same files,
compared by *content hash* rather than container bytes.  This means differing
compression (Spark vs ZIP, or two ZIPs with different deflate levels) or floppy
flux timing noise does not affect the result, because by the time Arcology has
produced ``ExtractedFile`` rows the logical content has been decoded out of the
container.

The metric is **size-weighted Jaccard** over the set of file content hashes:

    score = (sum of sizes of files present in BOTH) / (sum of sizes of files in EITHER)

so a tiny differing save-file barely dents the score while a changed main binary
moves it a lot.

Similarity is computed at two granularities:

* **Artefact level** -- the union of all the artefact's partitions.  Answers
  "are these two discs substantially the same?".
* **Component level** -- a directory subtree (a RISC OS ``!App`` or a top-level
  directory).  Answers "do these two discs each contain a similar copy of
  !ArtWorks?", which whole-disc comparison cannot, because the shared
  application is a small fraction of a populated hard-drive manifest.

Results are cached in the ``ArtefactSimilarity`` / ``ArtefactComponent`` /
``ComponentSimilarity`` tables, rebuilt by ``flask rebuild-similarity``.
"""

import math
from itertools import combinations
from flask import current_app
from sqlalchemy import func, or_
from arcology_shared.fuzzyhash import HAS_TLSH, TLSH_SIMILAR_DISTANCE, tlsh_diff
from ..database import (
    Artefact,
    ArtefactComponent,
    ArtefactSimilarity,
    ComponentSimilarity,
    ExtractedFile,
    Item,
    Partition,
    db,
)
from ..visibility import artefact_visibility_clause

# Pairs scoring below this size-weighted Jaccard floor are not cached, to keep
# the tables small.  Tune during evaluation.
MIN_STORE_SCORE = 0.10

# A directory subtree must contain at least this many hashable files to be
# treated as a comparable component.
MIN_COMPONENT_FILES = 2

# Deep-scan rule (B1): besides top-level dirs and RISC OS ``!`` apps, any
# directory no deeper than this whose subtree holds >= MIN_COMPONENT_FILES files
# is also a component.  This catches nested PC application folders
# (e.g. ``Program Files/Adobe/Photoshop``) so the same app is matched across
# discs that differ overall.  Depth is the number of path segments to the dir
# (a top-level dir is depth 1).
MAX_COMPONENT_DEPTH = 4

# Safety cap on components emitted per partition; if the deep scan produces more,
# the largest (by file count) are kept.  Bounds storage and pair generation on
# pathological trees.
MAX_COMPONENTS_PER_PARTITION = 500

# A content hash shared by more than this many artefacts is treated as
# ubiquitous "boilerplate" (common !System module, near-empty file, …): it does
# NOT generate candidate pairs on its own.  This both bounds the O(n^2)
# candidate-pair blow-up from one over-common file and stops two artefacts that
# share *only* such files being reported as similar.  Genuine matches still
# surface via their rarer shared files (and ubiquitous files still count toward
# the score of pairs found that way).
MAX_HASH_ARTEFACTS = 50

# Cap on extracted-file candidates scanned for a TLSH near-duplicate lookup.
TLSH_CANDIDATE_LIMIT = 5000


# ---------------------------------------------------------------------------
# Core metric
# ---------------------------------------------------------------------------

def weighted_jaccard(set_a: dict, set_b: dict, weights: dict | None = None) -> dict | None:
    """Size-weighted Jaccard between two ``{hash: size}`` maps.

    Returns a dict of metrics (``score``, ``shared_files``, ``union_files``,
    ``shared_bytes``, ``union_bytes``) or ``None`` if either set is empty.

    ``shared_bytes`` / ``union_bytes`` are always the raw (unweighted) byte sums
    for display.  When ``weights`` (a ``{hash: idf_weight}`` map) is supplied,
    only the ``score`` is rarity-weighted, so ubiquitous files contribute less.
    """
    if not set_a or not set_b:
        return None
    keys_a = set(set_a)
    keys_b = set(set_b)
    shared = keys_a & keys_b
    union = keys_a | keys_b

    def size_of(k):
        # Same content hash implies same size; take whichever set has it.
        return (set_a.get(k) if k in set_a else set_b.get(k)) or 0

    shared_bytes = sum(size_of(k) for k in shared)
    union_bytes = sum(size_of(k) for k in union)
    if weights is None:
        num, den = shared_bytes, union_bytes
    else:
        num = sum(size_of(k) * weights.get(k, 1.0) for k in shared)
        den = sum(size_of(k) * weights.get(k, 1.0) for k in union)
    score = (num / den) if den else 0.0
    return {
        "score": score,
        "shared_files": len(shared),
        "union_files": len(union),
        "shared_bytes": shared_bytes,
        "union_bytes": union_bytes,
    }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _hash_key():
    """SQL expression for a file's content key: sha256, falling back to md5."""
    return func.coalesce(ExtractedFile.sha256, ExtractedFile.md5)


def _file_rows_query():
    """Hashable, non-directory, non-empty extracted files across the collection.

    Yields ``(artefact_id, partition_id, path, hash, size)``.
    """
    hk = _hash_key()
    return (
        db.session.query(
            Partition.artefact_id,
            ExtractedFile.partition_id,
            ExtractedFile.path,
            hk.label("h"),
            ExtractedFile.file_size,
        )
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(ExtractedFile.is_directory.is_(False))
        .filter(hk.isnot(None))
        .filter(ExtractedFile.file_size.isnot(None))
        .filter(ExtractedFile.file_size > 0)
    )


def _partition_components(files):
    """Yield ``(root_path, member)`` components for one partition's files.

    ``files`` is a list of ``(path, hash, size)``.  Component roots are:
      1. top-level directories,
      2. the first ``!``-prefixed segment on a path (RISC OS application dirs),
      3. any directory no deeper than ``MAX_COMPONENT_DEPTH`` whose subtree holds
         >= ``MIN_COMPONENT_FILES`` files (catches nested PC app folders).

    Directories with identical content sets are de-duplicated to the shallowest
    (collapsing pass-through chains like ``Program Files/Adobe`` -> ``Adobe`` when
    the parent contains nothing else), and the result is capped at
    ``MAX_COMPONENTS_PER_PARTITION`` (largest kept).
    """
    # Subtree content set for every directory: each file belongs to all its
    # ancestor directories.
    dir_sets: dict[str, dict] = {}
    roots: set[str] = set()
    for path, h, size in files:
        segs = path.split("/")
        for i in range(1, len(segs)):  # ancestor dirs (exclude the file itself)
            dir_sets.setdefault("/".join(segs[:i]), {})[h] = size
        if len(segs) > 1:
            roots.add(segs[0])  # top-level directory
        acc: list[str] = []
        for seg in segs[:-1]:
            acc.append(seg)
            if seg.startswith("!"):  # RISC OS application directory
                roots.add("/".join(acc))
                break

    # Deep-scan rule: shallow-enough directories with enough content.
    for d, members in dir_sets.items():
        if d.count("/") + 1 <= MAX_COMPONENT_DEPTH and len(members) >= MIN_COMPONENT_FILES:
            roots.add(d)

    # Materialise, drop too-small, de-duplicate identical content sets.
    by_key: dict[frozenset, tuple] = {}
    for d in roots:
        members = dir_sets.get(d, {})
        if len(members) < MIN_COMPONENT_FILES:
            continue
        key = frozenset(members.items())
        depth = d.count("/")
        cur = by_key.get(key)
        if cur is None or depth < cur[0] or (depth == cur[0] and d < cur[1]):
            by_key[key] = (depth, d, members)

    components = [(d, members) for (_, d, members) in by_key.values()]
    if len(components) > MAX_COMPONENTS_PER_PARTITION:
        components.sort(key=lambda t: len(t[1]), reverse=True)
        components = components[:MAX_COMPONENTS_PER_PARTITION]
    yield from components


def _pairs_from_inverted(inverted: dict) -> set[tuple]:
    """All ``(a, b)`` (a < b) pairs that share at least one *distinctive* key.

    Buckets larger than ``MAX_HASH_ARTEFACTS`` are ubiquitous boilerplate and are
    skipped, which bounds the candidate-pair count and suppresses spurious
    matches on common files.
    """
    pairs: set[tuple] = set()
    for ids in inverted.values():
        if len(ids) < 2 or len(ids) > MAX_HASH_ARTEFACTS:
            continue
        for a, b in combinations(sorted(ids), 2):
            pairs.add((a, b))
    return pairs


# ---------------------------------------------------------------------------
# Rarity (IDF) weighting — optional, off by default
# ---------------------------------------------------------------------------

def _use_idf() -> bool:
    try:
        return bool(current_app.config.get("SIMILARITY_USE_IDF", False))
    except RuntimeError:  # no application context
        return False


def _idf_weights(df: dict, n_docs: int) -> dict:
    """Inverse-document-frequency weight per hash: rarer files weigh more."""
    if n_docs <= 0:
        return {}
    return {h: math.log(1.0 + n_docs / max(count, 1)) for h, count in df.items()}


def _total_artefact_docs() -> int:
    """Number of artefacts that have at least one hashable extracted file."""
    return (
        db.session.query(func.count(func.distinct(Partition.artefact_id)))
        .join(ExtractedFile, ExtractedFile.partition_id == Partition.id)
        .filter(ExtractedFile.is_directory.is_(False))
        .scalar()
    ) or 0


def _document_frequencies(hashes) -> dict:
    """Map each hash to the number of artefacts containing it (chunked)."""
    df: dict[str, int] = {}
    hk = _hash_key()
    hl = list(hashes)
    for i in range(0, len(hl), 500):
        chunk = hl[i:i + 500]
        rows = (
            db.session.query(hk, func.count(func.distinct(Partition.artefact_id)))
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .filter(ExtractedFile.is_directory.is_(False))
            .filter(hk.in_(chunk))
            .group_by(hk)
            .all()
        )
        for h, count in rows:
            df[h] = count
    return df


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

def rebuild_all() -> dict:
    """Recompute the entire similarity cache from extracted-file data."""
    rows = _file_rows_query().all()

    # Per-artefact content sets and an inverted hash -> artefacts index.
    artefact_sets: dict[int, dict] = {}
    inverted: dict[str, set] = {}
    for artefact_id, _partition_id, _path, h, size in rows:
        s = artefact_sets.setdefault(artefact_id, {})
        if h not in s:
            s[h] = size or 0
        inverted.setdefault(h, set()).add(artefact_id)

    # Optional rarity weighting from the in-memory inverted index.
    weights = None
    if _use_idf():
        df = {h: len(ids) for h, ids in inverted.items()}
        weights = _idf_weights(df, len(artefact_sets))

    ArtefactSimilarity.query.delete()
    db.session.flush()

    artefact_pairs = 0
    for a, b in _pairs_from_inverted(inverted):
        metrics = weighted_jaccard(artefact_sets[a], artefact_sets[b], weights)
        if metrics is None or metrics["score"] < MIN_STORE_SCORE:
            continue
        db.session.add(ArtefactSimilarity(artefact_a_id=a, artefact_b_id=b, **metrics))
        artefact_pairs += 1

    component_pairs = _rebuild_components(rows, weights)

    db.session.commit()
    return {"artefact_pairs": artefact_pairs, "component_pairs": component_pairs}


def _components_from_rows(rows):
    """Yield ``(partition_id, artefact_id, root_path, name, member)`` for each
    qualifying directory-subtree component, where ``member`` is ``{hash: size}``.

    ``rows`` is an iterable of ``(artefact_id, partition_id, path, hash, size)``.
    Shared by full rebuild and per-artefact incremental refresh.
    """
    by_partition: dict[int, list] = {}
    part_artefact: dict[int, int] = {}
    for artefact_id, partition_id, path, h, size in rows:
        by_partition.setdefault(partition_id, []).append((path, h, size or 0))
        part_artefact[partition_id] = artefact_id

    for partition_id, files in by_partition.items():
        for root, member in _partition_components(files):
            yield partition_id, part_artefact[partition_id], root, root.split("/")[-1], member


def _rebuild_components(rows, weights=None) -> int:
    """Rebuild component tables from the same file rows used for artefacts."""
    # CASCADE handles ComponentSimilarity, but delete explicitly for SQLite
    # where ondelete is not always enforced.
    ComponentSimilarity.query.delete()
    ArtefactComponent.query.delete()
    db.session.flush()

    component_sets: dict[int, dict] = {}
    component_artefact: dict[int, int] = {}
    inverted: dict[str, set] = {}

    for partition_id, artefact_id, root, name, member in _components_from_rows(rows):
        comp = ArtefactComponent(
            artefact_id=artefact_id,
            partition_id=partition_id,
            root_path=root,
            name=name,
            file_count=len(member),
            total_bytes=sum(member.values()),
        )
        db.session.add(comp)
        db.session.flush()  # assign comp.id
        component_sets[comp.id] = member
        component_artefact[comp.id] = artefact_id
        for h in member:
            inverted.setdefault(h, set()).add(comp.id)

    component_pairs = 0
    for a, b in _pairs_from_inverted(inverted):
        # Skip pairs within the same artefact (self-similar components are noise).
        if component_artefact[a] == component_artefact[b]:
            continue
        metrics = weighted_jaccard(component_sets[a], component_sets[b], weights)
        if metrics is None or metrics["score"] < MIN_STORE_SCORE:
            continue
        db.session.add(ComponentSimilarity(component_a_id=a, component_b_id=b, **metrics))
        component_pairs += 1

    return component_pairs


# ---------------------------------------------------------------------------
# Incremental refresh (one artefact at a time)
# ---------------------------------------------------------------------------

def _files_for_artefact(artefact_id):
    """File rows ``(artefact_id, partition_id, path, hash, size)`` for one artefact."""
    return _file_rows_query().filter(Partition.artefact_id == artefact_id).all()


def _files_for_artefacts(artefact_ids):
    """File rows for several artefacts in one (chunked) query.

    One round-trip instead of one per artefact; the caller derives both
    artefact-level and component-level content sets from these rows.
    """
    rows = []
    ids = list(artefact_ids)
    for i in range(0, len(ids), 500):  # stay under SQLite's bound-variable limit
        chunk = ids[i:i + 500]
        rows.extend(_file_rows_query().filter(Partition.artefact_id.in_(chunk)).all())
    return rows


def _member_set_from_rows(rows) -> dict:
    """Content set ``{hash: size}`` from ``(_, _, path, hash, size)`` file rows."""
    member: dict[str, int] = {}
    for _aid, _pid, _path, h, size in rows:
        if h not in member:
            member[h] = size or 0
    return member


def _component_set(files, root_path) -> dict:
    """Content set ``{hash: size}`` of the files under ``root_path``.

    ``files`` is a list of ``(path, hash, size)`` for the component's partition.
    """
    prefix = root_path + "/"
    return {
        h: (size or 0) for (path, h, size) in files
        if path == root_path or path.startswith(prefix)
    }


def _candidate_artefact_ids(artefact_id, hashes) -> set:
    """Other artefact ids sharing at least one of ``hashes`` (chunked IN query)."""
    out: set[int] = set()
    hk = _hash_key()
    hl = list(hashes)
    for i in range(0, len(hl), 500):  # stay under SQLite's bound-variable limit
        chunk = hl[i:i + 500]
        rows = (
            db.session.query(Partition.artefact_id)
            .join(ExtractedFile, ExtractedFile.partition_id == Partition.id)
            .filter(ExtractedFile.is_directory.is_(False))
            .filter(hk.in_(chunk))
            .filter(Partition.artefact_id != artefact_id)
            .distinct()
            .all()
        )
        out.update(r[0] for r in rows)
    return out


def _distinctive_hashes(df: dict) -> set:
    """Hashes that are not ubiquitous boilerplate (df <= MAX_HASH_ARTEFACTS)."""
    return {h for h, count in df.items() if count <= MAX_HASH_ARTEFACTS}


def recompute_for_artefact(artefact) -> dict:
    """Incrementally refresh all similarity rows involving one artefact.

    Deletes the artefact's existing artefact- and component-level rows, then
    recomputes them against the artefacts that share at least one file hash.
    Cheaper than a full ``rebuild_all`` and safe to call after each extraction.
    """
    aid = artefact.id

    # --- artefact level ---
    ArtefactSimilarity.query.filter(
        or_(ArtefactSimilarity.artefact_a_id == aid, ArtefactSimilarity.artefact_b_id == aid)
    ).delete(synchronize_session=False)

    my_rows = _files_for_artefact(aid)
    my_set = _member_set_from_rows(my_rows)
    # Document frequencies for this artefact's hashes drive both the
    # ubiquitous-file cap (candidate selection) and optional IDF weighting.
    df = _document_frequencies(my_set.keys()) if my_set else {}
    distinctive = _distinctive_hashes(df)
    candidate_ids = _candidate_artefact_ids(aid, distinctive) if distinctive else set()

    # Fetch every candidate artefact's files once, then derive both the
    # artefact-level sets and (below) the per-component sets from them — avoids a
    # query per candidate and a further query per candidate component.
    cand_rows = _files_for_artefacts(candidate_ids) if candidate_ids else []
    cand_sets: dict[int, dict] = {}          # artefact_id -> {hash: size}
    cand_partition_files: dict[int, list] = {}  # partition_id -> [(path, hash, size)]
    for artefact_id, partition_id, path, h, size in cand_rows:
        s = cand_sets.setdefault(artefact_id, {})
        if h not in s:
            s[h] = size or 0
        cand_partition_files.setdefault(partition_id, []).append((path, h, size or 0))

    weights = None
    if _use_idf() and my_set:
        # Extend df to every hash that appears in any candidate so union weights
        # are correct, then build IDF weights.
        extra = set()
        for s in cand_sets.values():
            extra.update(k for k in s if k not in df)
        if extra:
            df.update(_document_frequencies(extra))
        weights = _idf_weights(df, _total_artefact_docs())

    artefact_pairs = 0
    for oid in candidate_ids:
        metrics = weighted_jaccard(my_set, cand_sets.get(oid, {}), weights)
        if metrics is None or metrics["score"] < MIN_STORE_SCORE:
            continue
        lo, hi = (aid, oid) if aid < oid else (oid, aid)
        db.session.add(ArtefactSimilarity(artefact_a_id=lo, artefact_b_id=hi, **metrics))
        artefact_pairs += 1

    # --- component level ---
    my_comp_ids = [c.id for c in ArtefactComponent.query.filter_by(artefact_id=aid).all()]
    if my_comp_ids:
        ComponentSimilarity.query.filter(
            or_(
                ComponentSimilarity.component_a_id.in_(my_comp_ids),
                ComponentSimilarity.component_b_id.in_(my_comp_ids),
            )
        ).delete(synchronize_session=False)
        ArtefactComponent.query.filter_by(artefact_id=aid).delete(synchronize_session=False)
        db.session.flush()

    my_components = []  # (comp_row, member)
    for partition_id, _aid2, root, name, member in _components_from_rows(my_rows):
        comp = ArtefactComponent(
            artefact_id=aid,
            partition_id=partition_id,
            root_path=root,
            name=name,
            file_count=len(member),
            total_bytes=sum(member.values()),
        )
        db.session.add(comp)
        db.session.flush()
        my_components.append((comp, member))

    component_pairs = 0
    if my_components and candidate_ids:
        cand_comps = ArtefactComponent.query.filter(
            ArtefactComponent.artefact_id.in_(candidate_ids)
        ).all()
        comp_sets = {
            c.id: _component_set(cand_partition_files.get(c.partition_id, []), c.root_path)
            for c in cand_comps
        }
        for comp, member in my_components:
            for cc in cand_comps:
                metrics = weighted_jaccard(member, comp_sets[cc.id], weights)
                if metrics is None or metrics["score"] < MIN_STORE_SCORE or metrics["shared_files"] == 0:
                    continue
                lo, hi = (comp.id, cc.id) if comp.id < cc.id else (cc.id, comp.id)
                db.session.add(ComponentSimilarity(component_a_id=lo, component_b_id=hi, **metrics))
                component_pairs += 1

    db.session.commit()
    return {"artefact_pairs": artefact_pairs, "component_pairs": component_pairs}


# ---------------------------------------------------------------------------
# Query helpers (UI)
# ---------------------------------------------------------------------------

def similar_artefacts(artefact, viewer, *, limit=None, min_score=0.0):
    """Visible artefacts similar to ``artefact``, ordered by score descending.

    Returns a list of ``(other_artefact, ArtefactSimilarity)`` tuples.
    """
    sims = (
        ArtefactSimilarity.query
        .filter(
            or_(
                ArtefactSimilarity.artefact_a_id == artefact.id,
                ArtefactSimilarity.artefact_b_id == artefact.id,
            )
        )
        .filter(ArtefactSimilarity.score >= min_score)
        .all()
    )
    by_other: dict[int, ArtefactSimilarity] = {}
    for sim in sims:
        other = sim.artefact_b_id if sim.artefact_a_id == artefact.id else sim.artefact_a_id
        by_other[other] = sim
    if not by_other:
        return []

    visible = (
        Artefact.query
        .join(Item, Artefact.item_id == Item.id)
        .filter(Artefact.id.in_(by_other.keys()))
        .filter(artefact_visibility_clause(viewer))
        .all()
    )
    out = [(a, by_other[a.id]) for a in visible]
    out.sort(key=lambda t: t[1].score, reverse=True)
    if limit is not None:
        out = out[:limit]
    return out


def similar_components(artefact, viewer, *, min_score=0.0, per_component_limit=None):
    """Components of ``artefact`` that match components on other visible artefacts.

    Returns a list of ``(local_component, [(other_component, other_artefact,
    ComponentSimilarity), ...])`` ordered by best match descending.
    """
    comps = {c.id: c for c in ArtefactComponent.query.filter_by(artefact_id=artefact.id).all()}
    if not comps:
        return []

    sims = (
        ComponentSimilarity.query
        .filter(
            or_(
                ComponentSimilarity.component_a_id.in_(comps.keys()),
                ComponentSimilarity.component_b_id.in_(comps.keys()),
            )
        )
        .filter(ComponentSimilarity.score >= min_score)
        .all()
    )
    rels = []  # (local_id, other_id, sim)
    other_ids: set[int] = set()
    for sim in sims:
        if sim.component_a_id in comps:
            local_id, other_id = sim.component_a_id, sim.component_b_id
        else:
            local_id, other_id = sim.component_b_id, sim.component_a_id
        rels.append((local_id, other_id, sim))
        other_ids.add(other_id)
    if not other_ids:
        return []

    others = (
        db.session.query(ArtefactComponent, Artefact)
        .join(Artefact, ArtefactComponent.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ArtefactComponent.id.in_(other_ids))
        .filter(artefact_visibility_clause(viewer))
        .all()
    )
    other_map = {comp.id: (comp, art) for comp, art in others}

    grouped: dict[int, list] = {}
    for local_id, other_id, sim in rels:
        if other_id not in other_map:
            continue  # not visible to this viewer
        other_comp, other_art = other_map[other_id]
        grouped.setdefault(local_id, []).append((other_comp, other_art, sim))

    result = []
    for local_id, matches in grouped.items():
        matches.sort(key=lambda t: t[2].score, reverse=True)
        if per_component_limit is not None:
            matches = matches[:per_component_limit]
        result.append((comps[local_id], matches))
    result.sort(key=lambda t: t[1][0][2].score if t[1] else 0.0, reverse=True)
    return result


def component_match_counts(artefact_ids, viewer, *, min_score=0.0):
    """Map ``{root_path: (visible_match_count, component_uuid)}`` for the
    components of ``artefact_ids`` that have at least one visible match.

    Used to badge directories in the file browser.  Keyed by the component's
    partition-relative ``root_path`` so it lines up with browse paths.
    """
    comps = {
        c.id: c for c in
        ArtefactComponent.query.filter(ArtefactComponent.artefact_id.in_(list(artefact_ids))).all()
    }
    if not comps:
        return {}
    sims = (
        ComponentSimilarity.query
        .filter(
            or_(
                ComponentSimilarity.component_a_id.in_(comps.keys()),
                ComponentSimilarity.component_b_id.in_(comps.keys()),
            )
        )
        .filter(ComponentSimilarity.score >= min_score)
        .all()
    )
    rels = []  # (local_id, other_id)
    other_ids: set[int] = set()
    for sim in sims:
        if sim.component_a_id in comps:
            local_id, other_id = sim.component_a_id, sim.component_b_id
        else:
            local_id, other_id = sim.component_b_id, sim.component_a_id
        rels.append((local_id, other_id))
        other_ids.add(other_id)
    if not other_ids:
        return {}
    visible_others = {
        cid for (cid,) in
        db.session.query(ArtefactComponent.id)
        .join(Artefact, ArtefactComponent.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ArtefactComponent.id.in_(other_ids))
        .filter(artefact_visibility_clause(viewer))
        .all()
    }
    per_local: dict[int, set] = {}
    for local_id, other_id in rels:
        if other_id in visible_others:
            per_local.setdefault(local_id, set()).add(other_id)

    out: dict[str, tuple] = {}
    for local_id, others in per_local.items():
        comp = comps[local_id]
        # If two components share a root_path across artefacts in this view, keep
        # the larger match count.
        existing = out.get(comp.root_path)
        if existing is None or len(others) > existing[0]:
            out[comp.root_path] = (len(others), comp.uuid)
    return out


def matches_for_component(component, viewer, *, min_score=0.0):
    """Visible components similar to ``component``, best first.

    Returns ``[(other_component, other_artefact, ComponentSimilarity), ...]``.
    """
    sims = (
        ComponentSimilarity.query
        .filter(
            or_(
                ComponentSimilarity.component_a_id == component.id,
                ComponentSimilarity.component_b_id == component.id,
            )
        )
        .filter(ComponentSimilarity.score >= min_score)
        .all()
    )
    by_other: dict[int, ComponentSimilarity] = {}
    for sim in sims:
        other = sim.component_b_id if sim.component_a_id == component.id else sim.component_a_id
        by_other[other] = sim
    if not by_other:
        return []
    rows = (
        db.session.query(ArtefactComponent, Artefact)
        .join(Artefact, ArtefactComponent.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ArtefactComponent.id.in_(by_other.keys()))
        .filter(artefact_visibility_clause(viewer))
        .all()
    )
    out = [(comp, art, by_other[comp.id]) for comp, art in rows]
    out.sort(key=lambda t: t[2].score, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Layer 2 — byte-level fuzzy hash (TLSH) on individual extracted files
# ---------------------------------------------------------------------------

def similar_files_by_tlsh(source, viewer, *, max_distance=TLSH_SIMILAR_DISTANCE, limit=50):
    """Extracted files whose TLSH digest is within ``max_distance`` of ``source``.

    Answers "which one file changed between two otherwise-identical discs?".
    Returns ``[(ExtractedFile, Artefact, distance), ...]`` ordered by distance
    ascending (closest first).  Empty when TLSH is unavailable or ``source`` has
    no digest.  Exact-content duplicates (same sha256) are excluded — they belong
    in the existing exact "duplicates" view.
    """
    if not HAS_TLSH or not source.tlsh:
        return []

    q = (
        db.session.query(ExtractedFile, Artefact)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ExtractedFile.tlsh.isnot(None))
        .filter(ExtractedFile.id != source.id)
        .filter(ExtractedFile.is_directory.is_(False))
        .filter(artefact_visibility_clause(viewer))
    )
    if source.sha256:
        q = q.filter(or_(ExtractedFile.sha256.is_(None), ExtractedFile.sha256 != source.sha256))

    out = []
    for ef, art in q.limit(TLSH_CANDIDATE_LIMIT).all():
        dist = tlsh_diff(source.tlsh, ef.tlsh)
        if dist is None or dist > max_distance:
            continue
        out.append((ef, art, dist))
    out.sort(key=lambda t: t[2])
    return out[:limit]

# vim: ts=4 sw=4 et
