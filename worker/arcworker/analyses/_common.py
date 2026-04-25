"""
Shared infrastructure for analysis handlers.

Currently just the @analysis_handler decorator used by every handler in
the analyses subpackage.  Kept here (rather than in analysis.py) so that
handler modules can import it without pulling in the AnalysisWorker class.
"""

import functools
import json
import traceback

from ..config import log


def analysis_handler(description: str):
    """
    Decorator for analysis handler methods.

    Catches unhandled exceptions — including failures from the final
    update_analysis call inside the handler — and reports them to the API
    with a standard error format including traceback, preventing jobs from
    getting stuck in 'running' state.

    If the fallback failure report also fails (e.g. the server is down or
    still rejecting the payload), the error is logged and the function
    returns normally.  The job will remain in 'running' state in that case,
    but the worker will not loop or block on it.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, analysis: dict, artefact: dict, work_dir):
            analysis_id = analysis['id']
            analysis_uuid = analysis.get('uuid', '?')
            try:
                return fn(self, analysis, artefact, work_dir)
            except FileNotFoundError as e:
                # Expected when an artefact is deleted while jobs are
                # queued — the physical file is gone but the worker
                # already claimed the analysis.  Log a clean warning
                # instead of a full traceback.
                log.warning(
                    f"Analysis {analysis_id} ({analysis_uuid}) skipped: input file missing "
                    f"(artefact was probably deleted)"
                )
                try:
                    self.api.update_analysis(
                        analysis_id,
                        status='failed',
                        success=False,
                        error_message=f'Input file missing (artefact deleted?): {e}',
                    )
                except Exception:
                    pass  # API will 404 if analysis was cascade-deleted
            except Exception as e:
                log.exception(f"Analysis {analysis_id} ({analysis_uuid}) failed during {description}")
                try:
                    self.api.update_analysis(
                        analysis_id,
                        status='failed',
                        success=False,
                        error_message=f'{description} failed: {str(e)[:500]}',
                        details=json.dumps({
                            'exception': str(e),
                            'exception_trace': traceback.format_exc()[:5000],
                        })
                    )
                except Exception:
                    log.exception(
                        f"Analysis {analysis_id} ({analysis_uuid}): failed to report failure to API "
                        f"— job may remain in 'running' state"
                    )
        return wrapper
    return decorator
# vim: ts=4 sw=4 et
