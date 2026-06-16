"""
Shared infrastructure for analysis handlers.

Provides the @analysis_handler decorator used by every handler in the
analyses subpackage, and the HANDLERS registry it populates.  Kept here
(rather than in analysis.py) so that handler modules can import it
without pulling in the AnalysisWorker class.
"""

import functools
import json
import time
import traceback
from collections.abc import Callable
from arcology_shared.enums import AnalysisStatus, AnalysisType
from ..config import log
from ..exceptions import JobCancelledException

# AnalysisType.value → handler function, populated by @analysis_handler
# at import time.  Handlers are free functions with the signature
# ``(self, analysis, artefact, work_dir)``; the dispatch loop in
# AnalysisWorker.process_analysis() calls them with itself as ``self``.
HANDLERS: dict[str, Callable] = {}


class ProgressReporter:
    """Throttled progress reporter for long-running analysis handlers.

    A handler that iterates over many items (CLEANUP deleting hundreds of
    storage keys, an extraction walking thousands of files, …) can call
    :meth:`update` on every iteration; the reporter only forwards a progress
    summary to the server at most once every *min_interval* seconds, so tight
    loops do not flood the API.  The first :meth:`update` always emits so the
    UI leaves the bare "In progress…" state promptly.

    The summary is written via ``worker.report_progress`` (status stays
    RUNNING).  When the server reports the job is gone (deleted by a
    re-analyse race), :attr:`alive` flips to False and :meth:`update` returns
    False so the caller can stop early.

    Usage::

        reporter = ProgressReporter(self, analysis_id, total=n, label='Deleting')
        for i, item in enumerate(items, 1):
            ...
            if not reporter.update(i):
                break  # job was deleted server-side
    """

    def __init__(self, worker, analysis_id: int, total: int | None = None,
                 *, min_interval: float = 5.0, label: str = 'Processing'):
        self.worker = worker
        self.analysis_id = analysis_id
        self.total = total
        self.min_interval = min_interval
        self.label = label
        self.alive = True
        self._last_emit = 0.0  # monotonic time of last emit; 0 == never emitted

    def _format(self, done: int | None) -> str:
        if done is not None and self.total:
            pct = int(done * 100 / self.total)
            return f'{self.label}: {done} of {self.total} ({pct}%)'
        if done is not None:
            return f'{self.label}: {done}'
        return self.label

    def update(self, done: int | None = None, *, summary: str | None = None,
               force: bool = False) -> bool:
        """Maybe emit a progress summary; return :attr:`alive`.

        Throttled to one emit per *min_interval* seconds unless *force* is set
        (the first call always emits).  Pass *summary* to override the
        auto-formatted ``"<label>: <done> of <total> (<pct>%)"`` text.
        """
        if not self.alive:
            return False
        now = time.monotonic()
        if not force and self._last_emit and (now - self._last_emit) < self.min_interval:
            return True
        self._last_emit = now
        text = summary if summary is not None else self._format(done)
        if self.worker.report_progress(self.analysis_id, text) is False:
            self.alive = False
        return self.alive


def analysis_handler(description: str, analysis_type: AnalysisType | None = None):
    """
    Decorator for analysis handler methods.

    When *analysis_type* is given, registers the handler in ``HANDLERS``
    under its ``.value`` — this is the single wiring point for dispatch;
    no further registration is needed elsewhere.

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
            except JobCancelledException:
                log.info(
                    f"Analysis {analysis_id} ({analysis_uuid}) aborted (cancelled "
                    f"server-side) during {description}"
                )
                raise  # propagates to process_analysis() which handles it cleanly
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
                        status=AnalysisStatus.FAILED.value,
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
                        status=AnalysisStatus.FAILED.value,
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
        if analysis_type is not None:
            existing = HANDLERS.get(analysis_type.value)
            if existing is not None:
                raise RuntimeError(
                    f'Duplicate handler for {analysis_type.value}: '
                    f'{existing.__name__} and {fn.__name__}'
                )
            HANDLERS[analysis_type.value] = wrapper
        return wrapper
    return decorator


def resolve_extraction_file(self, extraction_path, db_path: str, work_dir,
                            path_prefix: str = '', risc_os_filetype=None):
    """Locate one extracted file on disk from its DB path.

    DB paths for archive-extracted files include the archive's own display
    path as a prefix (e.g. ``"z80Em/!Z80Em/Resources/AYSound"``) while on
    disk the file sits relative to the extraction root, so *path_prefix* is
    stripped first.  If that misses, the full DB path is retried — RISC OS
    archives often contain a top-level directory matching the archive
    filename, in which case the on-disk path retains the prefix.

    Returns ``(file_path, disk_relative_path)``; ``file_path`` is None when
    the file cannot be found under either path.
    """
    if path_prefix and db_path.startswith(path_prefix + '/'):
        disk_path = db_path[len(path_prefix) + 1:]
    else:
        disk_path = db_path

    file_path = self._resolve_single_extraction_file(
        extraction_path, disk_path, work_dir,
        risc_os_filetype=risc_os_filetype,
    )
    if file_path is None and disk_path != db_path:
        file_path = self._resolve_single_extraction_file(
            extraction_path, db_path, work_dir,
            risc_os_filetype=risc_os_filetype,
        )
    return file_path, disk_path


def find_extraction_path(self, artefact_uuid: str) -> str | None:
    """Locate an artefact's extraction output root by scanning its analyses.

    Fallback for jobs whose hints predate extraction_path being passed
    through directly.  Prefers FILE_EXTRACTION (always the disc-level
    extraction root); falls back to ARCHIVE_EXTRACT only when no
    file-extraction output exists.  Returns None when the analyses cannot
    be fetched or none has an output path.
    """
    analyses_resp = self.api.get(f"/artefacts/{artefact_uuid}/analysis")
    if not analyses_resp:
        return None
    file_extraction_path = None
    archive_extract_path = None
    for a in analyses_resp.get('analyses', []):
        atype = a.get('analysis_type')
        opath = a.get('output_path')
        if not opath:
            continue
        if atype == 'file_extraction' and not file_extraction_path:
            file_extraction_path = opath
        elif atype == 'archive_extract' and not archive_extract_path:
            archive_extract_path = opath
    return file_extraction_path or archive_extract_path
# vim: ts=4 sw=4 et
