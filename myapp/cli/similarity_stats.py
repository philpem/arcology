import json as _json
import click
from ..services.similarity_stats import collect_similarity_stats


def _fmt_bytes(n):
    n = n or 0
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def _hist(echo, hist, order=None):
    keys = order or sorted(hist)
    for k in keys:
        if k in hist:
            echo(f"    {k:>16}: {hist[k]}")


@click.command('similarity-stats')
@click.option('--top', type=int, default=30, show_default=True,
              help='How many ubiquitous hashes / top matches to list.')
@click.option('--json', 'as_json', is_flag=True,
              help='Emit the full structured report as JSON instead of text.')
def similarity_stats(top, as_json):
    """Read-only similarity diagnostics for the Phase 0 evaluation.

    Reports collection scale, per-artefact size distributions, candidate-pair
    cost, score / document-frequency histograms, the most ubiquitous files, and
    component coverage — the numbers needed to judge match quality and to choose
    the large-disc gate thresholds. Makes no changes to the database.

    Run after `rebuild-similarity` has populated the cache:

      docker compose exec web flask similarity-stats
      docker compose exec web flask similarity-stats --json > stats.json
    """
    stats = collect_similarity_stats(top_n=top)
    if as_json:
        click.echo(_json.dumps(stats, indent=2, default=str))
        return

    e = click.echo
    sc = stats['scale']
    e("=== Similarity evaluation (read-only) ===")
    e(f"Collection items: {stats['collection_total_items']}")
    e("")
    e("A. Scale")
    e(f"  Artefacts with hashable files: {sc['artefacts_with_hashable_files']}")
    e(f"  Hashable file rows:            {sc['hashable_file_rows']}")
    e(f"  Partitions:                    {sc['partitions']}")
    e(f"  Candidate pairs generated:     {sc['candidate_pairs_generated']}")
    e(f"  Artefact pairs stored:         {sc['artefact_pairs_stored']}")

    d = stats['distributions']

    def _dist(name, dist, as_bytes=False):
        f = _fmt_bytes if as_bytes else (lambda v: f"{v:.0f}")
        p = dist['percentiles']
        e(f"  {name}: n={dist['count']} mean={f(dist['mean'])} "
          f"p50={f(p[50])} p90={f(p[90])} p95={f(p[95])} p99={f(p[99])} max={f(p[100])}")

    e("")
    e("B. Per-artefact distributions (for the large-disc gate)")
    _dist("files/artefact      ", d['files_per_artefact'])
    _dist("distinct hashes/art ", d['distinct_hashes_per_artefact'])
    _dist("content bytes/art   ", d['content_bytes_per_artefact'], as_bytes=True)
    e("  Artefacts above N files:")
    for t, n in d['artefacts_above_file_count'].items():
        e(f"    > {t:>6}: {n}")

    e("")
    e("C. Pre-gate ratios over stored pairs (re-images cluster near 1.0)")
    order = [">=0.95", "0.80-0.95", "0.50-0.80", "<0.50"]
    e("  file-count ratio:")
    _hist(e, stats['pre_gate_ratios']['count_ratio'], order)
    e("  total-bytes ratio:")
    _hist(e, stats['pre_gate_ratios']['byte_ratio'], order)

    n = stats['noise']
    e("")
    e("D. Match quality / noise")
    e("  Score histogram:")
    _hist(e, n['score_histogram'],
          [">=0.90", "0.70-0.90", "0.50-0.70", "0.10-0.50", "<0.10"])
    e(f"  Distinct content hashes: {n['distinct_hashes']} "
      f"(unique to one artefact: {n['unique_hashes']}, "
      f"over cap >{stats['config']['MAX_HASH_ARTEFACTS']}: {n['hashes_over_cap']})")
    e("  df histogram (artefacts sharing a hash):")
    _hist(e, n['df_histogram'],
          ["1 (unique)", "2", "3-5",
           f"6-{stats['config']['MAX_HASH_ARTEFACTS']}",
           f">{stats['config']['MAX_HASH_ARTEFACTS']} (capped)"])
    e(f"  Most ubiquitous files (top {top}):")
    for h in n['top_ubiquitous']:
        e(f"    {h['artefact_count']:>5} discs  {_fmt_bytes(h['size']):>10}  "
          f"{h['sample_filename'] or '?'}")
    exc = n['exclusion']
    if exc['configured']:
        e(f"  Base-system exclusion: {exc['files_dropped']} files "
          f"({_fmt_bytes(exc['bytes_dropped'])}) on {exc['artefacts_affected']} "
          f"artefacts dropped via DB(s) {exc['excluded_db_ids']}")
    else:
        e("  Base-system exclusion: not configured")

    c = stats['components']
    e("")
    e("E. Components")
    e(f"  Total components: {c['total_components']}")
    e(f"  Component pairs stored: {c['component_pairs_stored']}")
    _dist("components/partition", c['components_per_partition'])
    e(f"  Artefacts with a component match but NO whole-disc match: "
      f"{c['component_only_artefacts']}")

    e("")
    e(f"F. Top {top} matches to hand-label (useful / borderline / noise)")
    for m in stats['top_matches']:
        e(f"  {m['score']:.3f}  {m['shared_files']}/{m['union_files']} files  "
          f"{m['a_label']}  <->  {m['b_label']}")
        e(f"         a={m['a_uuid']} b={m['b_uuid']}")

# vim: ts=4 sw=4 et
