import click
from ..services.similarity import rebuild_all


@click.command('rebuild-similarity')
def rebuild_similarity():
    """Rebuild the artefact content-set similarity cache.

    Recomputes size-weighted Jaccard similarity between artefacts (and between
    directory-subtree components) from the current ExtractedFile data, replacing
    the contents of the artefact_similarity / artefact_components /
    component_similarity tables.

    Run after extraction has populated file listings, or to refresh after
    importing more artefacts:

      docker compose exec web flask rebuild-similarity
    """
    click.echo("Rebuilding similarity cache ...")
    result = rebuild_all(progress=click.echo)
    click.echo(
        f"Done. {result['artefact_pairs']} artefact pair(s), "
        f"{result['component_pairs']} component pair(s), "
        f"{result.get('distinctiveness_rows', 0)} distinctiveness row(s) cached."
    )

# vim: ts=4 sw=4 et
