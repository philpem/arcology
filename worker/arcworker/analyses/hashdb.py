"""Hash rescan analysis handler.

HASH_RESCAN is a maintenance analysis that asks the web app to re-link
all extracted files for one artefact against the active hash databases.
The actual linking logic lives in the web app (myapp/utils/hash_rescan.py);
the worker simply triggers it via the API and records the result.
"""

import json
from pathlib import Path
from arcology_shared.enums import AnalysisType
from ._common import analysis_handler, run_step_loop

# Starting batch sizes for the bounded relink / recognition steps.  run_step_loop
# halves these on a server-signalled step timeout, so they are the *maximum* per
# request, not fixed sizes.
RECOGNITION_STEP_LIMIT = 25
LINK_STEP_LIMIT = 500


@analysis_handler("hash rescan", AnalysisType.HASH_RESCAN)
def process_hash_rescan(self, analysis: dict, artefact: dict, work_dir: Path):
    """Trigger a server-side hash rescan for this artefact."""
    analysis_id = analysis['id']
    artefact_uuid = artefact['uuid']

    result = self.api.run_hash_rescan(artefact_uuid)
    if result is None:
        self.fail_analysis(analysis_id, 'Hash rescan API call failed — no response from server')
        return

    updated = result.get('updated', 0)
    total = result.get('total', 0)
    recognition_queued = result.get('recognition_queued', 0)

    parts = [f'{updated}/{total} files linked']
    if recognition_queued:
        parts.append(f'{recognition_queued} product recognition job(s) queued')

    self.complete_analysis(
        analysis_id,
        summary=', '.join(parts),
        details=json.dumps(result),
    )


@analysis_handler("hashdb link", AnalysisType.HASHDB_LINK)
def process_hashdb_link(self, analysis: dict, artefact: dict, work_dir: Path):
    """Relink extracted files against a HashDB in bounded API steps."""
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    db_id = hints.get('database_id')
    if not db_id:
        self.fail_analysis(analysis_id, 'No database_id in analysis hints')
        return

    reporter = self.progress.start(label='Linking known files')
    result, totals = run_step_loop(
        lambda cursor, limit: self.api.hashdb_link_step(db_id, last_id=cursor, limit=limit),
        cursor_key='next_id',
        reporter=reporter,
        initial_limit=LINK_STEP_LIMIT,
    )
    if result is None:
        self.fail_analysis(analysis_id, 'HashDB link API call failed')
        return
    processed = totals.get('processed', 0)
    updated = totals.get('updated', 0)
    scanned = totals.get('matched_files_scanned', 0)

    parts = [f'{processed} known files checked', f'{updated}/{scanned} extracted files linked']
    if result.get('recognition_queued'):
        parts.append('product recognition backfill queued')
    self.complete_analysis(
        analysis_id,
        summary=', '.join(parts),
        details=json.dumps({
            'database_id': db_id,
            'known_files_processed': processed,
            'extracted_files_scanned': scanned,
            'updated': updated,
            'recognition_queued': bool(result.get('recognition_queued')),
        }),
    )


@analysis_handler("hashdb delete", AnalysisType.HASHDB_DELETE)
def process_hashdb_delete(self, analysis: dict, artefact: dict, work_dir: Path):
    """Reap a soft-deleted HashDB in bounded API steps.

    The web route already marked the database is_deleting / is_active=False and
    cancelled its own pending jobs; here we drive the server-side delete-step to
    completion (unlink extracted files, delete recognised_products / known_files
    / known_products, drop the database row, queue relinks of freed files).
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    db_id = hints.get('database_id')
    if not db_id:
        self.fail_analysis(analysis_id, 'No database_id in analysis hints')
        return

    reporter = self.progress.start(label='Deleting hash database')
    result, totals = run_step_loop(
        lambda cursor: self.api.hashdb_delete_step(db_id, cursor=cursor),
        cursor_key='cursor',
        reporter=reporter,
    )
    if result is None:
        self.fail_analysis(analysis_id, 'HashDB delete API call failed')
        return

    deleted = totals.get('deleted', 0)
    relinked = result.get('relinked_databases', 0)
    parts = [f'{deleted} row(s) deleted']
    if relinked:
        parts.append(f're-linking freed files against {relinked} other database(s)')
    self.complete_analysis(
        analysis_id,
        summary=', '.join(parts),
        details=json.dumps({
            'database_id': db_id,
            'rows_deleted': deleted,
            'relinked_databases': relinked,
        }),
    )


@analysis_handler("hashdb recognition", AnalysisType.HASHDB_RECOGNITION)
def process_hashdb_recognition(self, analysis: dict, artefact: dict, work_dir: Path):
    """Backfill product recognition for one HashDB in bounded API steps."""
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    db_id = hints.get('database_id')
    if not db_id:
        self.fail_analysis(analysis_id, 'No database_id in analysis hints')
        return

    reporter = self.progress.start(label='Recognising products')
    try:
        result, totals = run_step_loop(
            lambda cursor, limit: self.api.hashdb_recognition_step(
                db_id, last_product_id=cursor, limit=limit),
            cursor_key='next_product_id',
            reporter=reporter,
            initial_limit=RECOGNITION_STEP_LIMIT,
        )
        if result is None:
            msg = ('HashDB recognition could not complete (API failure, or a '
                   'product batch too large to process within the step time limit)')
            self.api.update_hashdb_recognition_status(db_id, 'failed', error=msg)
            self.fail_analysis(analysis_id, msg)
            return
    except Exception as exc:
        self.api.update_hashdb_recognition_status(
            db_id, 'failed', error=str(exc)[:1000]
        )
        raise
    processed = totals.get('processed', 0)
    matches = totals.get('matches', 0)
    skipped = totals.get('skipped', 0)

    summary = f'{processed} product(s) checked; {matches} recognition match(es) found'
    if skipped:
        summary += (f'; {skipped} product(s) skipped (too slow to process within '
                    f'the step time limit)')
    self.complete_analysis(
        analysis_id,
        summary=summary,
        details=json.dumps({
            'database_id': db_id,
            'products_processed': processed,
            'matches': matches,
            'products_skipped': skipped,
        }),
    )

# vim: ts=4 sw=4 et
