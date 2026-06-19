"""Content-set similarity refresh handler.

SIMILARITY_REFRESH recomputes one artefact's cached content-set similarity (and
its directory-subtree components) after its file listing changes.  The work is
pure database access, so — like the hashdb link/recognition jobs — it lives
server-side and the worker drives it as a bounded cursor loop, keeping the
recompute off the synchronous extraction-result request.
"""

import json
from pathlib import Path
from arcology_shared.enums import AnalysisType
from ._common import analysis_handler, run_step_loop

# Starting batch size (candidate artefacts per step); run_step_loop halves it on
# a server-signalled step timeout, so it is the maximum per request.
SIMILARITY_STEP_LIMIT = 200


@analysis_handler("similarity refresh", AnalysisType.SIMILARITY_REFRESH)
def process_similarity_refresh(self, analysis: dict, artefact: dict, work_dir: Path):
    """Refresh one artefact's content-set similarity in bounded API steps."""
    analysis_id = analysis['id']
    artefact_uuid = artefact.get('uuid')
    if not artefact_uuid:
        self.fail_analysis(analysis_id, 'No artefact for similarity refresh')
        return

    reporter = self.progress.start(label='Finding similar artefacts')
    result, totals = run_step_loop(
        lambda cursor, limit: self.api.similarity_step(
            artefact_uuid, cursor=cursor, limit=limit),
        cursor_key='next_cursor',
        reporter=reporter,
        initial_limit=SIMILARITY_STEP_LIMIT,
    )
    if result is None:
        self.fail_analysis(analysis_id, 'Similarity refresh API call failed')
        return

    processed = totals.get('processed', 0)
    artefact_pairs = totals.get('artefact_pairs', 0)
    component_pairs = totals.get('component_pairs', 0)
    self.complete_analysis(
        analysis_id,
        summary=(f'{processed} candidate(s) compared; {artefact_pairs} similar '
                 f'artefact(s), {component_pairs} similar component(s)'),
        details=json.dumps({
            'candidates_compared': processed,
            'artefact_pairs': artefact_pairs,
            'component_pairs': component_pairs,
        }),
    )

# vim: ts=4 sw=4 et
