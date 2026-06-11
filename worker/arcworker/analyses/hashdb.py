"""Hash rescan analysis handler.

HASH_RESCAN is a maintenance analysis that asks the web app to re-link
all extracted files for one artefact against the active hash databases.
The actual linking logic lives in the web app (myapp/utils/hash_rescan.py);
the worker simply triggers it via the API and records the result.
"""

import json
from pathlib import Path
from shared.enums import AnalysisType
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

# vim: ts=4 sw=4 et
