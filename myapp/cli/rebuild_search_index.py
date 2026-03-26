import json
import click
from ..extensions import db
from ..database import (
    Analysis, AnalysisType, AnalysisStatus,
    Partition, ArtefactProtection, ArtefactMastering,
)


def _process_analyses(analysis_type, label, row_handler):
    """Query completed analyses of a given type and process each with row_handler.

    Returns the number of rows created/updated by row_handler.
    """
    analyses = (
        Analysis.query
        .filter_by(
            analysis_type=analysis_type,
            status=AnalysisStatus.COMPLETED,
            success=True,
        )
        .all()
    )
    click.echo(f"Processing {len(analyses)} {label} analyses...")
    count = 0
    for analysis in analyses:
        if not analysis.details:
            continue
        try:
            details = json.loads(analysis.details)
        except (ValueError, TypeError):
            click.echo(f"  WARNING: could not parse details for analysis {analysis.uuid}", err=True)
            continue
        count += row_handler(analysis, details)
    return count


def _handle_protection(analysis, details):
    ArtefactProtection.query.filter_by(artefact_id=analysis.artefact_id).delete()
    count = 0
    for ind in details.get('indicators', []):
        db.session.add(ArtefactProtection(
            artefact_id=analysis.artefact_id,
            protection_type=ind.get('type', 'unknown'),
            track=ind.get('track'),
            side=ind.get('side'),
            details=ind.get('sector_id') or ind.get('details'),
        ))
        count += 1
    return count


def _handle_mastering(analysis, details):
    ArtefactMastering.query.filter_by(artefact_id=analysis.artefact_id).delete()
    count = 0
    for ind in details.get('indicators', []):
        _mtype = ind.get('type', 'unknown')
        if _mtype == 'bcd_timestamp':
            _mtype = 'formaster'
        db.session.add(ArtefactMastering(
            artefact_id=analysis.artefact_id,
            mastering_type=_mtype,
            track=ind.get('track'),
            decoded=ind.get('decoded') or ind.get('data'),
        ))
        count += 1
    return count


def _handle_partition(analysis, details):
    gnu_file_type = details.get('file', {}).get('file_type')
    if gnu_file_type:
        return (
            Partition.query
            .filter_by(artefact_id=analysis.artefact_id)
            .update({'gnu_file_type': gnu_file_type})
        )
    return 0


@click.command('rebuild-search-index')
def rebuild_search_index():
    """Rebuild the search index tables from completed analysis results.

    Reads all completed DISC_PROTECTION_DETECT, DISC_MASTERING_DETECT,
    and PARTITION_DETECT analyses and writes structured rows to:
      - artefact_protection
      - artefact_mastering
      - partitions.gnu_file_type

    The command is idempotent: existing rows are replaced on each run.
    Run this after applying the 20260309_000000 migration, or any time
    the search index needs to be rebuilt from scratch:

      docker compose exec web flask rebuild-search-index
    """
    prot_count = _process_analyses(
        AnalysisType.DISC_PROTECTION_DETECT,
        'DISC_PROTECTION_DETECT',
        _handle_protection,
    )
    mast_count = _process_analyses(
        AnalysisType.DISC_MASTERING_DETECT,
        'DISC_MASTERING_DETECT',
        _handle_mastering,
    )
    part_count = _process_analyses(
        AnalysisType.PARTITION_DETECT,
        'PARTITION_DETECT',
        _handle_partition,
    )

    db.session.commit()
    click.echo(
        f"Done. Protection indicators: {prot_count}, "
        f"mastering indicators: {mast_count}, "
        f"partitions updated: {part_count}."
    )

# vim: ts=4 sw=4 et
