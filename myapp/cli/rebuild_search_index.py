import click
from ..extensions import db
from ..services.search_index import rebuild_all


@click.command('rebuild-search-index')
def rebuild_search_index():
    """Rebuild the search index tables from completed analysis results.

    Reads all completed DISC_PROTECTION_DETECT, DISC_MASTERING_DETECT,
    PARTITION_DETECT, and RISCOS_MODULE_PARSE analyses and writes
    structured rows to:
      - artefact_protection
      - artefact_mastering
      - partitions.gnu_file_type
      - riscos_modules

    The command is idempotent: existing rows are replaced on each run.
    Run this after applying the 20260309_000000 migration, or any time
    the search index needs to be rebuilt from scratch:

      docker compose exec web flask rebuild-search-index
    """
    counts = rebuild_all(echo=click.echo)
    db.session.commit()
    click.echo(
        f"Done. Protection indicators: {counts.get('DISC_PROTECTION_DETECT', 0)}, "
        f"mastering indicators: {counts.get('DISC_MASTERING_DETECT', 0)}, "
        f"partitions updated: {counts.get('PARTITION_DETECT', 0)}, "
        f"RISC OS modules: {counts.get('RISCOS_MODULE_PARSE', 0)}."
    )

# vim: ts=4 sw=4 et
