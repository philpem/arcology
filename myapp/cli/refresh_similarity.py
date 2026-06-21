import click
from ..services.similarity import dirty_artefact_count, refresh_dirty


@click.command('refresh-similarity')
@click.option('--max-artefacts', type=int, default=None,
              help='Refresh at most this many stale artefacts (default: all).')
def refresh_similarity(max_artefacts):
    """Incrementally refresh the similarity cache for changed artefacts.

    Unlike `rebuild-similarity` (a full O(n^2) recompute), this drains only the
    artefacts whose extracted-file set has changed since their cache was last
    computed (the `similarity_dirty` flag), recomputing each one's similarity
    rows in full.  Suitable for running periodically from cron:

      docker compose exec web flask refresh-similarity

    Per-artefact recompute is exact for content changes; note that global
    parameter changes (a hashdb's exclude-from-similarity flag, SIMILARITY_USE_IDF)
    affect every score and still require a full `rebuild-similarity`.
    """
    pending = dirty_artefact_count()
    if not pending:
        click.echo("Similarity cache is up to date; nothing to refresh.")
        return
    click.echo(f"Refreshing similarity cache for {pending} changed artefact(s) ...")
    result = refresh_dirty(max_artefacts=max_artefacts, progress=click.echo)
    remaining = dirty_artefact_count()
    click.echo(
        f"Done. {result['artefacts']} artefact(s) refreshed "
        f"({result['artefact_pairs']} artefact pair(s), "
        f"{result['component_pairs']} component pair(s)); "
        f"{remaining} still pending."
    )

# vim: ts=4 sw=4 et
