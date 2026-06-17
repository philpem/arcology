"""Hash rescan analysis handler.

HASH_RESCAN is a maintenance analysis that asks the web app to re-link
all extracted files for one artefact against the active hash databases.
The actual linking logic lives in the web app (myapp/utils/hash_rescan.py);
the worker simply triggers it via the API and records the result.
"""

import json
from pathlib import Path
from arcology_shared.enums import AnalysisType
from ._common import analysis_handler


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

    last_id = 0
    processed = 0
    updated = 0
    scanned = 0
    reporter = self.progress.start(label='Linking known files')
    while True:
        result = self.api.hashdb_link_step(db_id, last_id=last_id)
        if result is None:
            self.fail_analysis(analysis_id, 'HashDB link API call failed')
            return
        processed += int(result.get('processed') or 0)
        updated += int(result.get('updated') or 0)
        scanned += int(result.get('matched_files_scanned') or 0)
        last_id = int(result.get('next_id') or last_id)
        reporter.update(processed)
        if result.get('done'):
            break

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


@analysis_handler("hashdb recognition", AnalysisType.HASHDB_RECOGNITION)
def process_hashdb_recognition(self, analysis: dict, artefact: dict, work_dir: Path):
    """Backfill product recognition for one HashDB in bounded API steps."""
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    db_id = hints.get('database_id')
    if not db_id:
        self.fail_analysis(analysis_id, 'No database_id in analysis hints')
        return

    last_product_id = 0
    processed = 0
    matches = 0
    reporter = self.progress.start(label='Recognising products')
    while True:
        result = self.api.hashdb_recognition_step(
            db_id,
            last_product_id=last_product_id,
        )
        if result is None:
            self.api.update_hashdb_recognition_status(
                db_id, 'failed', error='HashDB recognition API call failed'
            )
            self.fail_analysis(analysis_id, 'HashDB recognition API call failed')
            return
        processed += int(result.get('processed') or 0)
        matches += int(result.get('matches') or 0)
        last_product_id = int(result.get('next_product_id') or last_product_id)
        reporter.update(processed)
        if result.get('done'):
            break

    self.complete_analysis(
        analysis_id,
        summary=f'{processed} product(s) checked; {matches} recognition match(es) found',
        details=json.dumps({
            'database_id': db_id,
            'products_processed': processed,
            'matches': matches,
        }),
    )

# vim: ts=4 sw=4 et
