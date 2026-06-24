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
from typing import NamedTuple
from arcology_shared.enums import AnalysisStatus, AnalysisType
from arcology_shared.hints import HintKey
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

    def start(self, total: int | None = None, label: str | None = None):
        """Set the total and/or label once the work size is known.

        Handy when the framework injects a reporter before the handler knows
        how many items it will process: ``self.progress.start(total=n,
        label='Hashing').update(0)``.  Returns ``self`` so it can be chained.

        A change of *label* marks a new phase (e.g. Hashing → Registering on the
        same reused reporter): the throttle is reset so the next ``update`` emits
        immediately, otherwise a phase shorter than *min_interval* could be
        entirely throttled out and the UI would keep showing the previous
        phase's label/percentage.
        """
        if total is not None:
            self.total = total
        if label is not None and label != self.label:
            self.label = label
            self._last_emit = 0.0  # new phase → let the next update emit at once
        return self

    def _format(self, done: int | None) -> str:
        if done is not None and self.total:
            pct = int(done * 100 / self.total)
            return f'{self.label}: {done} of {self.total} ({pct}%)'
        if done is not None:
            return f'{self.label}: {done}'
        return self.label

    def update(self, done: int | None = None, *, message: str | None = None,
               force: bool = False) -> bool:
        """Maybe emit progress (message + current/total); return :attr:`alive`.

        Throttled to one emit per *min_interval* seconds unless *force* is set
        (the first call always emits).  *done* feeds both the formatted message
        and the numeric ``progress_current``; pass *message* to override the
        auto-formatted ``"<label>: <done> of <total> (<pct>%)"`` text.
        """
        if not self.alive:
            return False
        now = time.monotonic()
        if not force and self._last_emit and (now - self._last_emit) < self.min_interval:
            return True
        self._last_emit = now
        text = message if message is not None else self._format(done)
        if self.worker.report_progress(
                self.analysis_id, message=text,
                current=done, total=self.total) is False:
            self.alive = False
        return self.alive


def run_step_loop(step, *, cursor_key, reporter=None, initial_limit=None,
                  min_limit=1):
    """Drive a worker-triggered, server-side **bounded-step** job to completion.

    This is the worker half of a recurring pattern in this codebase: work that
    is pure database access (relinking known files, product recognition) lives
    server-side next to the data, and the worker drives it as a cursor loop so
    that no single web request runs long.  Each call processes one capped batch
    and returns ``done`` plus a ``next_<...>`` cursor; the worker keeps calling
    until ``done``.  See ``myapp/services/recognition.py`` and the
    ``*-step`` endpoints in ``myapp/blueprints/api.py``.

    *step* is invoked as ``step(cursor)`` (cursor starts at 0) and must return
    the server's JSON result dict, or ``None`` on transport failure.  The
    ``next_<...>`` cursor field is named by *cursor_key*.  Every integer field
    other than the cursor is summed across steps into *totals*; the running
    ``processed`` total drives the optional *reporter*.

    **Adaptive batch size.**  When *initial_limit* is given the step is invoked
    as ``step(cursor, limit)`` and the loop adapts the batch to server
    back-pressure (AIMD-style): a step that returns ``{'timed_out': True}`` — the
    server aborted an over-long batch — is retried at the *same* cursor with
    *limit* halved, down to *min_limit*; each successful step doubles *limit*
    back up toward *initial_limit* so throughput recovers after a slow patch.
    At *min_limit* the server is expected to make progress itself (e.g. skip a
    single un-processable unit and advance the cursor); a step that *still*
    reports ``timed_out`` at *min_limit* cannot be subdivided and fails the loop
    (``last_result`` of ``None``).  When *initial_limit* is None the step is
    invoked as ``step(cursor)`` and a ``timed_out`` result is an unrecoverable
    failure.

    Termination is guarded defensively: a step that returns a non-dict body, or
    that fails to advance the cursor without signalling ``done``, is treated as
    a failed step (``last_result`` of ``None``) rather than spinning the worker
    thread forever or crashing it — the contract is owned by the server
    endpoints, but a buggy/contract-violating response must not hang the job.

    Returns ``(last_result, totals)``.  ``last_result`` is ``None`` when a step
    failed — the caller decides how to fail the analysis (and, for recognition
    backfills, how to report a failed status).
    """
    cursor = 0
    limit = initial_limit
    totals: dict[str, int] = {}
    while True:
        result = step(cursor, limit) if limit is not None else step(cursor)
        if not isinstance(result, dict):
            return None, totals
        if result.get('timed_out'):
            if limit is None or limit <= min_limit:
                # Cannot subdivide further — a single batch element is too slow
                # to process within the server's step time limit.  Fail rather
                # than retry the same over-long batch forever.
                return None, totals
            limit = max(min_limit, limit // 2)
            continue  # retry the same cursor with a smaller batch
        # The server may supply a human-readable progress label and total (e.g.
        # the database name and product count) so the status line can read
        # "Recognising products in HashDB 'Arcarc Apps': 625 of 3456".  These are
        # relayed to the reporter, not summed into totals.
        if reporter is not None:
            if result.get('progress_label'):
                reporter.label = result['progress_label']
            if result.get('progress_total') is not None:
                reporter.total = result['progress_total']
        for key, value in result.items():
            if key in (cursor_key, 'progress_label', 'progress_total'):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
        if reporter is not None:
            reporter.update(totals.get('processed', 0))
        if result.get('done'):
            return result, totals
        # A step succeeded (it advanced or signalled a server-side skip), so ramp
        # the batch back up toward initial_limit.  Without this the limit would
        # stay stuck at the size it was halved to — e.g. 1 product per request
        # for the rest of a backfill after one slow product forced a skip.
        if limit is not None and limit < initial_limit:
            limit = min(initial_limit, limit * 2)
        next_cursor = result.get(cursor_key, cursor)
        if next_cursor == cursor:
            # The cursor failed to advance and the step did not signal done:
            # a contract violation that would otherwise loop forever.
            return None, totals
        cursor = next_cursor


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


class BatchScanResult(NamedTuple):
    """Result of :func:`scan_partition_files`.

    ``files`` is the directory-excluded, ``select_files``-filtered list of
    file_data dicts from the partition.  ``extraction_path`` is resolved only
    when at least one file matched (lazy fallback) — when ``files`` is empty it
    carries whatever the hint held (possibly ``None``), so a handler that
    short-circuits on ``not scan.files`` never triggers a needless
    ``find_extraction_path`` round-trip.
    """

    extraction_path: str | None
    files: list[dict]
    path_prefix: str
    partition_uuid: str | None


def scan_partition_files(self, analysis: dict, artefact: dict, *, select_files):
    """Shared per-partition batch scaffold: hint parse → file list → filter → path.

    The common front half of every "scan a partition's extracted files and do
    X to each" handler (FORMAT_CONVERT Mode 2, REPLAY_TRANSCODE,
    RISCOS_MODULE_PARSE, MEDIA_TRANSCODE).  Parses the standard extraction
    hints, fetches the partition file list via the API (pushing the
    extraction-context filter down), drops directories, and keeps the files for
    which ``select_files(file_data)`` is truthy.

    *select_files* is ``(file_data) -> bool``; directories are excluded before
    it is called.  It may also record side data keyed by ``file_data['path']``
    (e.g. FORMAT_CONVERT's viewable-type map) as a side effect.

    Returns a :class:`BatchScanResult`, or ``None`` when ``partition_uuid`` is
    missing or the extraction path cannot be resolved — the caller fails the
    analysis with its own message.  This helper never calls
    ``complete_analysis``/``fail_analysis`` or queues anything.

    The extraction-path fallback (``find_extraction_path``) is resolved lazily:
    only when ≥1 file matched.  When nothing matched, the hint's path (or
    ``None``) is returned alongside ``files=[]`` so the caller's "no files"
    short-circuit runs before the path is ever needed.
    """
    hints = json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get(HintKey.PARTITION_UUID)
    extraction_path = hints.get(HintKey.EXTRACTION_PATH)
    path_prefix = hints.get(HintKey.PATH_PREFIX, '')

    if not partition_uuid:
        return None

    base_params = {'show_known': 'true'}
    if path_prefix:
        base_params['path_prefix'] = path_prefix
    else:
        base_params['extraction_depth'] = 0

    all_files = self.api.get_partition_files(partition_uuid, **base_params)

    files = [
        f for f in all_files
        if not f.get('is_directory', False) and select_files(f)
    ]

    # Lazy path resolution: a handler that finds no matching files short-circuits
    # before using extraction_path, so skip the find_extraction_path round-trip.
    if files and not extraction_path:
        extraction_path = find_extraction_path(self, artefact.get('uuid'))
        if not extraction_path:
            return None

    return BatchScanResult(
        extraction_path=extraction_path,
        files=files,
        path_prefix=path_prefix,
        partition_uuid=partition_uuid,
    )


def iter_resolved_files(self, files, extraction_path, work_dir, *,
                        path_prefix='', reporter=None, on_missing=None):
    """Yield ``(file_data, file_path, disk_path)`` for each resolvable file.

    The shared back half of the batch scaffold: wraps
    :func:`resolve_extraction_file` (passing the file's ``risc_os_filetype`` and
    the *path_prefix*) for every file in *files*.  Files that cannot be located
    on disk are skipped after invoking ``on_missing(file_data, db_path)`` (so a
    handler can bucket them into its own error list).  When *reporter* is given
    it is driven by the count of successfully-resolved files (not the raw scan
    position), so progress does not skip values when files are missing.
    """
    resolved = 0
    for file_data in files:
        db_path = file_data['path']
        file_path, disk_path = resolve_extraction_file(
            self, extraction_path, db_path, work_dir,
            path_prefix=path_prefix,
            risc_os_filetype=file_data.get('risc_os_filetype') or None,
        )
        if file_path is None:
            if on_missing is not None:
                on_missing(file_data, db_path)
            continue
        resolved += 1
        if reporter is not None:
            reporter.update(resolved)
        yield file_data, file_path, disk_path
# vim: ts=4 sw=4 et
