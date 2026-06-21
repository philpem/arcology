"""Read-only diagnostics for the content-set similarity feature.

`collect_similarity_stats()` gathers the numbers needed to evaluate similarity on
a real collection (the "Phase 0" evaluation) and to choose the thresholds the
later cost-control phases introduce.  It performs **no writes** — it reads the
current ``ExtractedFile`` data and the existing similarity cache.

The single file-scan it does mirrors what ``rebuild_all`` does (build per-artefact
content sets and an inverted hash -> artefacts index), so its memory footprint is
the same as a rebuild; it just measures instead of comparing-and-storing.
"""

from sqlalchemy import func
from ..database import (
    Artefact,
    ArtefactComponent,
    ArtefactSimilarity,
    ComponentSimilarity,
    ExtractedFile,
    HashDatabase,
    Item,
    KnownFile,
    Partition,
    db,
)
from .similarity import (
    MAX_HASH_ARTEFACTS,
    MIN_STORE_SCORE,
    _file_rows_query,
    _hash_key,
    _pairs_from_inverted,
)

# Percentiles reported for every distribution.
_PERCENTILES = (50, 90, 95, 99, 100)
# File-count thresholds the large-disc gate (Phase 2) would care about.
_FILE_COUNT_THRESHOLDS = (1000, 5000, 10000, 50000)


def _percentile(sorted_vals, p):
    """Nearest-rank percentile of an already-sorted list (no numpy)."""
    if not sorted_vals:
        return 0
    if p >= 100:
        return sorted_vals[-1]
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def _distribution(values):
    """Summarise a list of numbers: count, total, mean, and percentiles."""
    vals = sorted(values)
    n = len(vals)
    total = sum(vals)
    return {
        "count": n,
        "total": total,
        "mean": (total / n) if n else 0,
        "percentiles": {p: _percentile(vals, p) for p in _PERCENTILES},
    }


def _ratio_bucket(r):
    if r >= 0.95:
        return ">=0.95"
    if r >= 0.80:
        return "0.80-0.95"
    if r >= 0.50:
        return "0.50-0.80"
    return "<0.50"


def _score_bucket(s):
    if s >= 0.90:
        return ">=0.90"
    if s >= 0.70:
        return "0.70-0.90"
    if s >= 0.50:
        return "0.50-0.70"
    if s >= 0.10:
        return "0.10-0.50"
    return "<0.10"


def _df_bucket(df):
    if df <= 1:
        return "1 (unique)"
    if df == 2:
        return "2"
    if df <= 5:
        return "3-5"
    if df <= MAX_HASH_ARTEFACTS:
        return f"6-{MAX_HASH_ARTEFACTS}"
    return f">{MAX_HASH_ARTEFACTS} (capped)"


def _excluded_db_ids():
    """IDs of hash databases flagged exclude_from_similarity, or [] if the
    feature (Phase 1) is not present in this build."""
    if not hasattr(HashDatabase, "exclude_from_similarity"):
        return []
    return [
        row[0]
        for row in db.session.query(HashDatabase.id)
        .filter(HashDatabase.exclude_from_similarity.is_(True))
        .all()
    ]


def _exclusion_stats():
    """How much content a base-system exclusion (Phase 1) drops, if configured."""
    excluded = _excluded_db_ids()
    if not excluded:
        return {"configured": False, "excluded_db_ids": []}
    # Hashable, non-empty files linked as known to an excluded database.
    hk = _hash_key()
    rows = (
        db.session.query(Partition.artefact_id, ExtractedFile.file_size)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
        .filter(ExtractedFile.is_directory.is_(False))
        .filter(hk.isnot(None))
        .filter(ExtractedFile.file_size.isnot(None))
        .filter(ExtractedFile.file_size > 0)
        .filter(KnownFile.database_id.in_(excluded))
        .all()
    )
    affected = {aid for aid, _ in rows}
    return {
        "configured": True,
        "excluded_db_ids": excluded,
        "files_dropped": len(rows),
        "bytes_dropped": sum(sz or 0 for _aid, sz in rows),
        "artefacts_affected": len(affected),
    }


def _top_ubiquitous(inverted, limit):
    """The most-common content hashes, with a sample filename for each."""
    top = sorted(inverted.items(), key=lambda kv: len(kv[1]), reverse=True)[:limit]
    out = []
    hk = _hash_key()
    for h, ids in top:
        sample = (
            db.session.query(ExtractedFile.filename, ExtractedFile.file_size)
            .filter(hk == h)
            .first()
        )
        out.append({
            "hash": h,
            "artefact_count": len(ids),
            "sample_filename": sample[0] if sample else None,
            "size": sample[1] if sample else None,
        })
    return out


def _component_only_artefacts():
    """Artefacts that have a component match but no whole-disc match.

    This is the cross-disc value the large-disc gate (Phase 2) leans on: a disc
    that shares an app (a component) with another without being a whole-disc
    match at all.
    """
    whole = set()
    for a, b in db.session.query(
        ArtefactSimilarity.artefact_a_id, ArtefactSimilarity.artefact_b_id
    ).all():
        whole.add(a)
        whole.add(b)
    comp_ids = set()
    for a, b in db.session.query(
        ComponentSimilarity.component_a_id, ComponentSimilarity.component_b_id
    ).all():
        comp_ids.add(a)
        comp_ids.add(b)
    if not comp_ids:
        return 0, len(whole)
    comp_artefacts = {
        row[0]
        for row in db.session.query(ArtefactComponent.artefact_id)
        .filter(ArtefactComponent.id.in_(comp_ids))
        .all()
    }
    return len(comp_artefacts - whole), len(whole)


def _sample_top_matches(limit):
    """Top stored artefact matches by score, for manual precision labelling."""
    rows = (
        ArtefactSimilarity.query
        .order_by(ArtefactSimilarity.score.desc())
        .limit(limit)
        .all()
    )
    labels = {}
    ids = {r.artefact_a_id for r in rows} | {r.artefact_b_id for r in rows}
    if ids:
        for art in (
            db.session.query(Artefact.id, Artefact.uuid, Artefact.label)
            .filter(Artefact.id.in_(ids))
            .all()
        ):
            labels[art[0]] = (art[1], art[2])
    out = []
    for r in rows:
        a = labels.get(r.artefact_a_id, (None, "?"))
        b = labels.get(r.artefact_b_id, (None, "?"))
        out.append({
            "score": r.score,
            "shared_files": r.shared_files,
            "union_files": r.union_files,
            "a_uuid": a[0], "a_label": a[1],
            "b_uuid": b[0], "b_label": b[1],
        })
    return out


def collect_similarity_stats(top_n=30):
    """Gather the full Phase-0 evaluation report as a structured dict.

    Read-only.  ``top_n`` controls how many ubiquitous hashes and top matches are
    listed for inspection.
    """
    # One pass over the hashable file rows -> per-artefact content sets + inverted
    # hash -> artefacts index + raw per-artefact file counts (same shape as the
    # rebuild, but we only measure).
    artefact_sets: dict[int, dict] = {}
    file_counts: dict[int, int] = {}
    partitions: set[int] = set()
    inverted: dict[str, set] = {}
    n_file_rows = 0
    for artefact_id, partition_id, _path, h, size in _file_rows_query().yield_per(5000):
        n_file_rows += 1
        partitions.add(partition_id)
        s = artefact_sets.setdefault(artefact_id, {})
        if h not in s:
            s[h] = size or 0
        file_counts[artefact_id] = file_counts.get(artefact_id, 0) + 1
        inverted.setdefault(h, set()).add(artefact_id)

    n_artefacts = len(artefact_sets)

    # A. scale
    candidate_pairs = len(_pairs_from_inverted(inverted))
    stored_pairs = db.session.query(func.count(ArtefactSimilarity.id)).scalar() or 0
    scale = {
        "artefacts_with_hashable_files": n_artefacts,
        "hashable_file_rows": n_file_rows,
        "partitions": len(partitions),
        "candidate_pairs_generated": candidate_pairs,
        "artefact_pairs_stored": stored_pairs,
    }

    # B. per-artefact size distributions
    fc_sorted = sorted(file_counts.values())
    above = {t: sum(1 for c in fc_sorted if c > t) for t in _FILE_COUNT_THRESHOLDS}
    distributions = {
        "files_per_artefact": _distribution(file_counts.values()),
        "distinct_hashes_per_artefact": _distribution([len(s) for s in artefact_sets.values()]),
        "content_bytes_per_artefact": _distribution(
            [sum(s.values()) for s in artefact_sets.values()]),
        "artefacts_above_file_count": above,
    }

    # C. pre-gate ratios over stored pairs
    count_ratio_hist: dict[str, int] = {}
    byte_ratio_hist: dict[str, int] = {}
    for a, b in db.session.query(
        ArtefactSimilarity.artefact_a_id, ArtefactSimilarity.artefact_b_id
    ).all():
        sa, sb = artefact_sets.get(a), artefact_sets.get(b)
        if not sa or not sb:
            continue
        ca, cb = len(sa), len(sb)
        ba, bb = sum(sa.values()), sum(sb.values())
        cr = min(ca, cb) / max(ca, cb) if max(ca, cb) else 0
        br = min(ba, bb) / max(ba, bb) if max(ba, bb) else 0
        count_ratio_hist[_ratio_bucket(cr)] = count_ratio_hist.get(_ratio_bucket(cr), 0) + 1
        byte_ratio_hist[_ratio_bucket(br)] = byte_ratio_hist.get(_ratio_bucket(br), 0) + 1
    pre_gate = {"count_ratio": count_ratio_hist, "byte_ratio": byte_ratio_hist}

    # D. score histogram, df distribution, ubiquitous files, exclusion impact
    score_hist: dict[str, int] = {}
    for (score,) in db.session.query(ArtefactSimilarity.score).all():
        score_hist[_score_bucket(score)] = score_hist.get(_score_bucket(score), 0) + 1
    df_hist: dict[str, int] = {}
    unique_hashes = 0
    over_cap = 0
    for ids in inverted.values():
        df = len(ids)
        df_hist[_df_bucket(df)] = df_hist.get(_df_bucket(df), 0) + 1
        if df <= 1:
            unique_hashes += 1
        if df > MAX_HASH_ARTEFACTS:
            over_cap += 1
    noise = {
        "score_histogram": score_hist,
        "df_histogram": df_hist,
        "distinct_hashes": len(inverted),
        "unique_hashes": unique_hashes,
        "hashes_over_cap": over_cap,
        "top_ubiquitous": _top_ubiquitous(inverted, top_n),
        "exclusion": _exclusion_stats(),
    }

    # E. components
    comp_per_partition = [
        row[1]
        for row in db.session.query(
            ArtefactComponent.partition_id, func.count(ArtefactComponent.id)
        ).group_by(ArtefactComponent.partition_id).all()
    ]
    component_only, _whole = _component_only_artefacts()
    components = {
        "total_components": db.session.query(func.count(ArtefactComponent.id)).scalar() or 0,
        "component_pairs_stored": db.session.query(func.count(ComponentSimilarity.id)).scalar() or 0,
        "components_per_partition": _distribution(comp_per_partition),
        "component_only_artefacts": component_only,
    }

    return {
        "config": {"MIN_STORE_SCORE": MIN_STORE_SCORE, "MAX_HASH_ARTEFACTS": MAX_HASH_ARTEFACTS},
        "scale": scale,
        "distributions": distributions,
        "pre_gate_ratios": pre_gate,
        "noise": noise,
        "components": components,
        "top_matches": _sample_top_matches(top_n),
        "collection_total_items": db.session.query(func.count(Item.id)).scalar() or 0,
    }

# vim: ts=4 sw=4 et
