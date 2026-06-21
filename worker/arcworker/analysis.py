"""
Analysis worker and job processing.

Contains the main AnalysisWorker class that polls for jobs and
dispatches them to handlers.  Each handler lives in
``worker.arcworker.analyses`` and is bound to AnalysisWorker as a
class attribute at the bottom of this module so the function bodies
(which already reference ``self``) work unchanged.
"""

import contextlib
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES, AnalysisStatus
from arcology_shared.hints import HintKey

try:
    import sentry_sdk
except ImportError:
    # Sentry is optional — only installed/initialised when SENTRY_DSN is set
    # (see config.py).  Absence must not stop the worker from running.
    sentry_sdk = None

from . import analyses as _analyses
from .analyses._common import ProgressReporter
from .api import ArcologyAPI
from .cache_keys import artefact_cache_prefix, partition_cache_relpath
from .compression import decompress_if_needed
from .config import (
    CANCEL_CHECK_INTERVAL,
    HEARTBEAT_MAX_SECONDS,
    POLL_BACKOFF_CEILING,
    POLL_BACKOFF_FLOOR,
    STALE_RESET_INTERVAL,
    log,
)
from .exceptions import JobCancelledException
from .tools.base import clear_cancel_event, set_cancel_event
from .utils.text import make_latin1_fspath

_COMPRESS_SUFFIXES = frozenset({'.gz', '.bz2', '.zst'})


@contextlib.contextmanager
def _analysis_transaction(analysis_type, artefact_uuid):
    """Wrap one analysis job in a Sentry transaction; no-op when Sentry is absent.

    sentry_sdk is an optional import (see top of module).  Even when it is
    installed but not initialised (no SENTRY_DSN), start_transaction is itself a
    no-op, so this only has to special-case the not-installed path.
    """
    if sentry_sdk is None:
        yield
        return
    with sentry_sdk.start_transaction(op="arcology.analysis", name=analysis_type) as txn:
        txn.set_tag("analysis_type", analysis_type)
        txn.set_tag("artefact_uuid", artefact_uuid)
        yield


def _inner_format_extension(filename: str) -> str:
    """Return the inner (non-compression) extension of filename, or ''.

    'file.dfi.bz2' -> '.dfi', 'file.scp.gz' -> '.scp', 'file.bz2' -> ''
    """
    lower = filename.lower()
    for suffix in _COMPRESS_SUFFIXES:
        if lower.endswith(suffix):
            lower = lower[:-len(suffix)]
            break
    ext = Path(lower).suffix
    # Don't return another compression suffix as the "inner" extension.
    return ext if ext not in _COMPRESS_SUFFIXES else ''


class AnalysisWorker:
    """Main worker class that processes analysis jobs."""

    # Per-job progress reporter, (re)created in process_analysis() before each
    # handler runs.  Declared at class level so it always resolves (handlers may
    # call self.progress.update(...)) and so test doubles built with
    # MagicMock(spec=AnalysisWorker) expose it.
    progress: "ProgressReporter | None" = None

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path,
                 api_key: str = '', storage=None,
                 analysis_types: list[str] | None = None):
        """
        Initialize the worker.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded artefacts are stored
            output_dir: Directory for analysis outputs (e.g., visualisations)
            api_key: Worker API key for authentication
            storage: StorageBackend instance (if None, creates from config)
            analysis_types: Optional list of AnalysisType names this worker
                            will claim.  None/empty = claim all types.
        """
        self.uploads = upload_dir
        self.outputs = output_dir
        self.outputs.mkdir(parents=True, exist_ok=True)

        if storage is None:
            from arcology_shared.storage import create_storage
            from .config import (
                S3_ACCESS_KEY,
                S3_BUCKET,
                S3_ENDPOINT_URL,
                S3_PUBLIC_URL,
                S3_REGION,
                S3_SECRET_KEY,
                S3_UPLOAD_CONCURRENCY,
                STORAGE_BACKEND,
            )
            storage = create_storage({
                'STORAGE_BACKEND': STORAGE_BACKEND,
                'S3_ENDPOINT_URL': S3_ENDPOINT_URL,
                'S3_BUCKET': S3_BUCKET,
                'S3_ACCESS_KEY': S3_ACCESS_KEY,
                'S3_SECRET_KEY': S3_SECRET_KEY,
                'S3_REGION': S3_REGION,
                'S3_PUBLIC_URL': S3_PUBLIC_URL,
                'S3_UPLOAD_CONCURRENCY': S3_UPLOAD_CONCURRENCY,
                'UPLOAD_FOLDER': str(upload_dir),
                'OUTPUT_FOLDER': str(output_dir),
            })
        self.storage = storage

        self.api = ArcologyAPI(api_url, upload_dir, output_dir,
                               api_key=api_key, storage=storage)
        self.analysis_types: list[str] = list(analysis_types) if analysis_types else []
        # Control-plane / DB-only analyses are owned by the taskrunner container
        # (myapp/taskrunner) and are hard-excluded here regardless of any
        # WORKER_ANALYSIS_TYPES opt-in, so the worker and taskrunner never both
        # claim them.  Names (uppercase) match the DB enum the API filters on.
        self._control_plane_names = frozenset(
            t.name for t in CONTROL_PLANE_ANALYSIS_TYPES)
        self._decompression_info = None  # Set by get_input_path() when decompression occurs

        # Set when SIGTERM/SIGINT is received so the main loop finishes the
        # current job and then exits cleanly instead of being SIGKILL'd by
        # Docker.  An Event (rather than a bare bool) lets idle polling sleeps
        # wake immediately on shutdown.
        self._shutdown = threading.Event()

    def get_input_path(self, artefact: dict, work_dir: Path) -> Path:
        """
        Get input file path, decompressing if needed.

        In S3 mode, downloads the file from storage to work_dir first.
        After calling this method, self._decompression_info is set to a dict
        with decompression details if the file was compressed, or None otherwise.
        Handlers can use this to include decompression info in their output.

        Args:
            artefact: Artefact dict from API
            work_dir: Working directory for decompression

        Returns:
            Path to the (decompressed) input file

        Raises:
            FileNotFoundError: If input file doesn't exist
        """
        storage_path = artefact['storage_path']
        storage_directory = artefact.get('storage_directory', 'uploads')

        key = self.storage.storage_key(storage_directory, storage_path)

        from arcology_shared.storage import LocalStorage
        if isinstance(self.storage, LocalStorage):
            # Local mode: use direct filesystem path
            input_path = self.storage.local_path(key)
        else:
            # S3 mode: download to work_dir
            input_path = work_dir / storage_path
            if not input_path.exists():
                log.info(f"Downloading input file from storage: {key}")
                self.storage.get(key, input_path)  # raises FileNotFoundError on 404

        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Save stats before decompression — the compressed file may be deleted
        # during decompression (e.g. in S3 mode where the downloaded file IS
        # the compressed copy that gets cleaned up).
        compressed_name = input_path.name
        compressed_size = input_path.stat().st_size
        compression_format = input_path.suffix.lower()

        result = decompress_if_needed(input_path, work_dir)

        # Restore the inner format extension so tools that use extension-based
        # format detection (e.g. hxcfe detecting DFI vs SCP) work correctly.
        # The storage key carries only the compression suffix (e.g. hash.bz2),
        # so after decompression the file has no inner extension (e.g. just
        # "hash").  Derive the correct extension from original_filename.
        original_filename = artefact.get('original_filename', '')
        if original_filename:
            inner_ext = _inner_format_extension(original_filename)
            if inner_ext and result.suffix.lower() != inner_ext:
                linked = result.with_suffix(inner_ext)
                if not linked.exists():
                    linked.symlink_to(result.name)  # relative symlink, same dir
                result = linked

        # Track decompression metadata for handlers that need it
        if result != input_path:
            self._decompression_info = {
                'was_decompressed': True,
                'compressed_name': compressed_name,
                'compressed_size': compressed_size,
                'decompressed_name': result.name,
                'decompressed_size': result.stat().st_size,
                'compression_format': compression_format,
            }
        else:
            self._decompression_info = None

        return result

    def save_output_file(self, source_path: Path, filename: str, subdir: str | None = None) -> str:
        """
        Save an output file (like a visualisation) to storage.

        Args:
            source_path: Path to the generated file
            filename: Destination filename
            subdir: Optional subdirectory within outputs (e.g. '{item_uuid}_{item_slug}/{artefact_uuid}_{artefact_slug}')

        Returns:
            The relative path for use in URLs (subdir/filename or just filename)
        """
        if subdir:
            relative_path = f"{subdir}/{filename}"
        else:
            relative_path = filename
        key = self.storage.storage_key('outputs', relative_path)
        self.storage.put(key, source_path)
        return relative_path

    def fail_analysis(self, analysis_id: int, error_message: str, **kwargs):
        """Report a failed analysis to the API."""
        self.api.update_analysis(
            analysis_id,
            status=AnalysisStatus.FAILED.value,
            success=False,
            error_message=error_message,
            **kwargs,
        )

    def _resolve_partition_image(
        self,
        partition_image_path: str | None,
        artefact: dict,
        work_dir: Path,
    ) -> Path:
        """Resolve a cached partition image path, downloading from S3 if needed.

        PARTITION_DETECT caches decompressed partition images locally and uploads
        them to S3.  Downstream handlers (FILE_EXTRACTION, ARMLOCK_REMOVE) receive
        the local path as a hint, but on a different worker or after a restart the
        file may only exist in S3.

        Resolution order:
        1. Hint path exists locally → use it directly.
        2. S3 storage active → download from S3 cache to work_dir.
        3. Hint absent or not found anywhere → fall back to get_input_path().

        Args:
            partition_image_path: Value of the 'partition_image_path' hint, or None.
            artefact: Artefact dict from the API (needs 'uuid').
            work_dir: Temporary working directory for S3 downloads.

        Returns:
            Path to the resolved input file.
        """
        if partition_image_path:
            local_path = Path(partition_image_path)
            if local_path.exists():
                log.info(f"Using cached partition image: {local_path}")
                return local_path
            # S3 mode or different worker: try to download from storage cache
            from arcology_shared.storage import S3Storage
            if isinstance(self.storage, S3Storage):
                try:
                    rel = local_path.relative_to(self.outputs)
                    cache_key = self.storage.storage_key('outputs', str(rel))
                except ValueError:
                    cache_key = self.storage.storage_key(
                        'outputs',
                        partition_cache_relpath(artefact['uuid'], local_path.name))
                dest = work_dir / local_path.name
                try:
                    self.storage.get(cache_key, dest)
                    log.info(f"Downloaded cached partition image from storage: {cache_key}")
                    return dest
                except FileNotFoundError:
                    pass  # not cached yet; fall through to get_input_path
        return self.get_input_path(artefact, work_dir)

    def report_progress(self, analysis_id: int, *, message: str | None = None,
                        current: int | None = None, total: int | None = None,
                        heartbeat: bool = False) -> bool:
        """Report interim progress (or a bare heartbeat) for a running analysis.

        Writes the structured progress fields (and bumps the server-side
        progress_updated_at) without changing status, so a long-running job
        (a large CLEANUP, a multi-thousand-file extraction, ...) shows live
        progress in the queue/detail UI instead of an opaque spinner — and is
        not mistaken for a stuck job by heartbeat-based stale detection.

        Pass ``heartbeat=True`` with no fields to refresh the liveness
        timestamp only (used by the cancellation-monitor thread to cover
        black-box phases where the handler reports nothing).

        Best-effort: a failed progress update must never fail an otherwise
        healthy job, so transient API errors are swallowed and logged at debug
        level.  Returns False only on the definitive "gone" signal (the row was
        deleted server-side, e.g. by a re-analyse race) so callers can stop
        early; True otherwise.
        """
        payload: dict = {}
        if message is not None:
            payload['progress_message'] = message
        if current is not None:
            payload['progress_current'] = current
        if total is not None:
            payload['progress_total'] = total
        if heartbeat:
            payload['heartbeat'] = True
        if not payload:
            return True
        try:
            return self.api.update_analysis(analysis_id, **payload)
        except Exception as e:
            log.debug(f"Progress update for analysis {analysis_id} failed (ignored): {e}")
            return True

    def complete_analysis(self, analysis_id: int, summary: str | None = None, **kwargs) -> bool:
        """Report a completed successful analysis to the API.

        Returns False when the analysis no longer exists server-side (the
        "gone" signal from update_analysis); True otherwise.
        """
        payload = {
            'status': AnalysisStatus.COMPLETED.value,
            'success': True,
            **kwargs,
        }
        if summary is not None:
            payload['summary'] = summary
        return self.api.update_analysis(analysis_id, **payload)

    def queue_file_extraction(
        self,
        artefact_uuid: str,
        filesystem: str,
        partition_index: int,
        *,
        partition_image_path: str | None = None,
        container_format: str | None = None,
    ):
        """Queue FILE_EXTRACTION with the standard hint structure."""
        from arcology_shared.enums import AnalysisType
        hints = {
            HintKey.FILESYSTEM: filesystem,
            HintKey.PARTITION_INDEX: partition_index,
        }
        if partition_image_path:
            hints[HintKey.PARTITION_IMAGE_PATH] = partition_image_path
        if container_format:
            hints[HintKey.CONTAINER_FORMAT] = container_format
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.FILE_EXTRACTION.value,
            hints=hints,
        )

    def queue_partition_follow_ups(
        self,
        artefact_uuid: str,
        partition_uuid: str,
        *,
        extraction_path: str | None = None,
        path_prefix: str | None = None,
    ):
        """Queue the standard archive-detect, product-recognition, and format-convert follow-ups."""
        from arcology_shared.enums import AnalysisType
        archive_hints = {HintKey.PARTITION_UUID: partition_uuid}
        if extraction_path:
            archive_hints[HintKey.EXTRACTION_PATH] = extraction_path
        if path_prefix:
            archive_hints[HintKey.PATH_PREFIX] = path_prefix
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.ARCHIVE_DETECT.value,
            hints=archive_hints,
        )
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.PRODUCT_RECOGNITION.value,
            hints={HintKey.PARTITION_UUID: partition_uuid},
        )
        if extraction_path:
            self.api.queue_analysis(
                artefact_uuid,
                AnalysisType.FORMAT_CONVERT.value,
                hints={
                    HintKey.EXTRACTION_PATH: extraction_path,
                    HintKey.PARTITION_UUID: partition_uuid,
                },
            )
        # RISC OS module metadata extraction — harmless no-op on non-Acorn
        # extractions since only filetype ffa files are scanned.
        module_hints = {HintKey.PARTITION_UUID: partition_uuid}
        if extraction_path:
            module_hints[HintKey.EXTRACTION_PATH] = extraction_path
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.RISCOS_MODULE_PARSE.value,
            hints=module_hints,
        )
        # Acorn Replay / ARMovie metadata — harmless no-op on extractions with
        # no filetype ae7 files.
        replay_hints = {HintKey.PARTITION_UUID: partition_uuid}
        if extraction_path:
            replay_hints[HintKey.EXTRACTION_PATH] = extraction_path
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.REPLAY_PROCESS.value,
            hints=replay_hints,
        )

    def _relative_output_path(self, extract_dir: Path) -> str:
        """Convert an absolute extraction directory to a relative path for storage.

        Returns the path relative to the outputs directory, suitable for
        storing in Analysis.output_path and as a storage key prefix.
        """
        try:
            return str(extract_dir.relative_to(self.outputs))
        except ValueError:
            # Not under outputs dir — return as-is (shouldn't happen normally)
            return str(extract_dir)

    def _upload_extraction_tree(self, extract_dir: Path) -> None:
        """Upload an extraction directory tree to storage (S3 mode).

        In local mode this is a no-op since the files are already in place.
        In S3 mode, uploads the tree then removes the local copy.
        """
        from arcology_shared.storage import LocalStorage
        if isinstance(self.storage, LocalStorage):
            return
        rel = self._relative_output_path(extract_dir)
        prefix = self.storage.storage_key('outputs', rel)
        # Report upload progress so a large tree (tens of thousands of files)
        # doesn't leave the status line frozen on the previous "Registering
        # files … 100%" phase while the objects are pushed to S3.  Heartbeating
        # here also keeps the job from being reset as stale during a long upload.
        progress = getattr(self, 'progress', None)
        progress_callback = None
        if progress is not None:
            def progress_callback(done, total):
                progress.start(
                    total=total, label='Uploading extracted files').update(done)
        # Upload first; only remove the local copy on success.  If put_tree()
        # raises, the exception propagates (the analysis handler marks the job
        # failed), and the local directory is left in place so a retry can
        # re-upload without orphaning partially-uploaded S3 objects.
        count = self.storage.put_tree(prefix, extract_dir, progress_callback=progress_callback)
        log.info(f"Uploaded {count} files to storage prefix: {prefix}")
        shutil.rmtree(extract_dir, ignore_errors=True)
        log.info(f"Cleaned up local extraction directory: {extract_dir}")

    def _resolve_single_extraction_file(
        self,
        extraction_path: str,
        relative_path: str,
        dest_dir: Path,
        risc_os_filetype: str | None = None,
    ) -> Path | None:
        """Download a single file from an extraction tree.

        In local mode, resolves the path on the local filesystem.
        In S3 mode, downloads only the one file needed instead of
        the entire extraction tree.

        Tries RISC OS filetype suffix variants (,ddc / ,DDC) when
        risc_os_filetype is provided, then the plain name as fallback.

        Returns the local path to the file, or None if not found.
        """
        from arcology_shared.storage import LocalStorage

        if isinstance(self.storage, LocalStorage):
            path = Path(extraction_path)
            if not path.is_absolute():
                path = self.outputs / extraction_path
            base = path / relative_path

            # Build candidates list (filetype suffix variants + plain)
            candidates = []
            if risc_os_filetype:
                candidates.append(Path(str(base) + ',' + risc_os_filetype.lower()))
                candidates.append(Path(str(base) + ',' + risc_os_filetype.upper()))
            candidates.append(base)

            # Also try Latin-1 byte variants for each candidate
            all_candidates = []
            for candidate in candidates:
                all_candidates.append(candidate)
                latin1_variant = make_latin1_fspath(str(candidate))
                if latin1_variant is not None:
                    all_candidates.append(Path(latin1_variant))

            for candidate in all_candidates:
                if candidate.exists():
                    return candidate
            return None

        # S3 mode: try downloading each candidate key directly.
        # Using get() with a 404 catch is one S3 call per attempt vs
        # exists() + get() = two calls for the successful candidate.
        prefix = extraction_path.lstrip('/')
        base_key = f"outputs/{prefix}/{relative_path.lstrip('/')}"

        candidates = []
        if risc_os_filetype:
            candidates.append(base_key + ',' + risc_os_filetype.lower())
            candidates.append(base_key + ',' + risc_os_filetype.upper())
        candidates.append(base_key)

        for key in candidates:
            local_path = dest_dir / Path(key).name
            try:
                self.storage.get(key, local_path)
                log.debug(f"Downloaded single file from S3: {key}")
                return local_path
            except FileNotFoundError:
                local_path.unlink(missing_ok=True)
                continue

        return None

    def _cleanup_partition_cache(self, artefact_uuid: str) -> None:
        """Remove local partition cache for an artefact if no jobs still need it.

        In S3 mode the cache is already in object storage, so the local copy
        is only kept as a convenience for subsequent analyses on the same
        worker.  Once all analyses for the artefact have finished (no pending
        or running jobs remain), the local cache directory is safe to delete.

        In local mode this is a no-op — the cache is the permanent copy.
        """
        from arcology_shared.storage import LocalStorage
        if isinstance(self.storage, LocalStorage):
            return

        cache_dir = self.outputs / artefact_cache_prefix(artefact_uuid)
        if not cache_dir.exists():
            return

        # Ask the API whether any analyses are still pending or running
        try:
            resp = self.api.get(f'/artefacts/{artefact_uuid}/analysis')
            if resp and 'analyses' in resp:
                active = [
                    a for a in resp['analyses']
                    if a.get('status') in (AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value)
                ]
                if active:
                    log.debug(
                        f"Keeping partition cache for {artefact_uuid}: "
                        f"{len(active)} analyses still active"
                    )
                    return
        except Exception as e:
            log.warning(f"Could not check analysis status for cache cleanup: {e}")
            return

        shutil.rmtree(cache_dir, ignore_errors=True)
        log.info(f"Cleaned up local partition cache: {cache_dir}")

    # =========================================================================
    # Analysis Handlers
    # =========================================================================
    #
    # The handler functions live in ``worker.arcworker.analyses.*`` and are
    # dispatched via the ``_analyses.HANDLERS`` registry, populated by the
    # ``@analysis_handler(description, AnalysisType.X)`` decorator — no
    # per-handler wiring is needed here.
    #
    # The private helpers and data tables below are bound as class
    # attributes because handler bodies reference them via ``self.``.
    # Static helpers (``_sniff_archive_magic``, ``_is_riscos_zip``) are
    # wrapped in ``staticmethod`` so callsites like
    # ``self._sniff_archive_magic(p)`` don't pass ``self`` to them.

    # File / archive extraction
    _extract_top_level_archive    = _analyses._extract_top_level_archive
    _handle_disk_image_bundle     = _analyses._handle_disk_image_bundle
    _sniff_archive_magic          = staticmethod(_analyses._sniff_archive_magic)
    _is_riscos_zip                = staticmethod(_analyses._is_riscos_zip)
    _PROMOTABLE_EXTENSIONS        = _analyses._PROMOTABLE_EXTENSIONS

    # Format conversion (Sprite / Draw / Text / images)
    _convert_file_to_outputs      = _analyses._convert_file_to_outputs
    _detect_viewable_type         = _analyses._detect_viewable_type
    _RISCOS_VIEWABLE_SUFFIXES     = _analyses._RISCOS_VIEWABLE_SUFFIXES
    _EXT_VIEWABLE                 = _analyses._EXT_VIEWABLE
    _RISCOS_HEX_VIEWABLE          = _analyses._RISCOS_HEX_VIEWABLE

    # =========================================================================
    # Job Processing
    # =========================================================================

    def _monitor_cancellation(self, analysis_uuid: str, analysis_id: int,
                              stop_event: threading.Event) -> None:
        """Daemon thread: poll API every CANCEL_CHECK_INTERVAL seconds.

        Sets the module-level cancel event when the job is gone (404) or is no
        longer in 'running' state, so that run_tool() / stream_to_file() can
        abort the current subprocess promptly.

        While the job is still running, also sends a best-effort heartbeat so
        that a long job which reports no item-level progress (e.g. a black-box
        extraction or ffmpeg pass) is not mistaken for a stuck job by
        heartbeat-based stale detection.  This heartbeat is capped at
        HEARTBEAT_MAX_SECONDS: it only proves the *process* is alive, not that
        the handler is progressing, so a handler wedged on an unbounded call must
        not be kept "fresh" forever — past the cap the heartbeat stops and the
        server's stale reset can recover the job.  Handlers that are genuinely
        progressing bump progress_updated_at directly (ProgressReporter), so the
        cap never causes a falsely-progressing job to be reset.
        """
        monitor_started = time.monotonic()
        while not stop_event.wait(timeout=CANCEL_CHECK_INTERVAL):
            try:
                resp = self.api._request_response('get', f'/analysis/{analysis_uuid}')
            except Exception as e:
                # Network glitch: don't cancel, just retry next interval.
                log.debug(f"Cancel monitor for {analysis_uuid}: API check failed ({e}), will retry")
                continue

            # The job may have finished (stop requested) while this request was
            # in flight. Setting the shared cancel event now could abort the
            # *next* job, so bail out without signalling.
            if stop_event.is_set():
                return

            if resp.status_code == 404:
                log.info(
                    f"Analysis {analysis_uuid} no longer exists — "
                    f"signalling cancellation"
                )
                set_cancel_event()
                return
            if resp.status_code == 200:
                try:
                    status = resp.json().get('status', '')
                except ValueError:
                    continue  # non-JSON body, treat as transient
                if status not in (AnalysisStatus.RUNNING.value,):
                    log.info(
                        f"Analysis {analysis_uuid} status changed to "
                        f"{status!r} — signalling cancellation"
                    )
                    set_cancel_event()
                    return
                # Still running — refresh the liveness timestamp, but only while
                # under the heartbeat cap (process-liveness alone must not keep a
                # wedged, non-progressing job alive indefinitely).
                if time.monotonic() - monitor_started < HEARTBEAT_MAX_SECONDS:
                    self.report_progress(analysis_id, heartbeat=True)
            # Any other HTTP status: don't cancel.

    def process_analysis(self, analysis: dict):
        """Process a single analysis job."""
        analysis_id = analysis['id']
        analysis_uuid = analysis.get('uuid', '?')
        analysis_type = analysis['analysis_type']
        # CLEANUP jobs have no artefact; the API serialises the key as
        # null, so coerce None (not just a missing key) to an empty dict.
        artefact = analysis.get('artefact') or {}

        log.info(f"Processing analysis {analysis_id} ({analysis_uuid}): {analysis_type} for {artefact.get('label', 'unknown')}")

        # Status is already RUNNING from the atomic claim in claim_and_process().

        # Framework-injected progress reporter for this job.  Handlers report
        # item-level progress via self.progress.start(total=...).update(done);
        # those that don't still get the auto-heartbeat from the monitor thread.
        # Safe as instance state because each worker process runs exactly one
        # job at a time (horizontal scaling is multiple worker containers).
        self.progress = ProgressReporter(self, analysis_id)

        # Clear any stale cancel signal from a previous job, then start the
        # monitoring thread that will re-set it if this job is cancelled remotely
        # (and heartbeats the job so a long run isn't reset as stale).
        clear_cancel_event()
        stop_monitor = threading.Event()
        monitor_thread = threading.Thread(
            target=self._monitor_cancellation,
            args=(analysis_uuid, analysis_id, stop_monitor),
            daemon=True,
            name=f'cancel-monitor-{analysis_id}',
        )
        monitor_thread.start()

        try:
            # Create temporary work directory
            with tempfile.TemporaryDirectory(prefix=f'arcology_{analysis_id}_') as work_dir:
                work_path = Path(work_dir)

                try:
                    # Dispatch to appropriate handler (registered by the
                    # @analysis_handler decorator; called with this worker
                    # as ``self``)
                    handler = _analyses.HANDLERS.get(analysis_type)
                    if handler:
                        with _analysis_transaction(analysis_type, artefact.get('uuid', '')):
                            handler(self, analysis, artefact, work_path)
                    else:
                        log.warning(f"Unknown analysis type: {analysis_type}")
                        self.fail_analysis(analysis_id, f'Unknown analysis type: {analysis_type}')

                except JobCancelledException:
                    # Already logged by @analysis_handler.  Do NOT call fail_analysis()
                    # — the analysis row has been deleted (or replaced) server-side.
                    log.info(
                        f"Analysis {analysis_id} ({analysis_uuid}) aborted: "
                        f"job was cancelled server-side"
                    )
                except Exception as e:
                    if sentry_sdk is not None:
                        sentry_sdk.capture_exception(e)
                    log.exception(f"Analysis {analysis_id} ({analysis_uuid}) failed with exception")
                    try:
                        self.fail_analysis(analysis_id, str(e)[:1000])
                    except Exception:
                        log.exception(
                            f"Analysis {analysis_id} ({analysis_uuid}): failed to report failure to API "
                            f"— job may remain in 'running' state"
                        )
                finally:
                    # In S3 mode, clean up the local partition cache once all
                    # analyses for this artefact have finished.
                    try:
                        self._cleanup_partition_cache(artefact['uuid'])
                    except Exception:
                        pass  # Best-effort cleanup, don't block on errors
        finally:
            stop_monitor.set()
            monitor_thread.join(timeout=5.0)
            self.progress = None

    def _effective_types(self) -> list[str]:
        """The explicit allow-list of AnalysisType names this worker may claim.

        Starts from the WORKER_ANALYSIS_TYPES opt-in (or *all* types when unset)
        and always removes the control-plane / DB-only types owned by the
        taskrunner.  Returned as an explicit list (never ``None``/empty so the
        server-side filter cannot fall back to "all types") — belt-and-suspenders
        so even a stray control-plane row is never handed to the worker.
        """
        from arcology_shared.enums import AnalysisType
        base = self.analysis_types or [t.name for t in AnalysisType]
        return [t for t in base if t not in self._control_plane_names]

    def claim_and_process(self) -> int:
        """
        Atomically claim a pending analysis and process it.

        This is safe for multiple workers - each worker claims one job at a time
        by setting status to 'running' before processing. The API ensures only
        one worker can claim each job.

        Returns:
            Number of analyses processed (0 or 1)
        """
        analyses = self.api.get_pending_analyses(
            analysis_types=self._effective_types()
        )

        if not analyses:
            return 0

        # Try to claim the first available job
        for analysis in analyses:
            analysis_id = analysis['id']
            analysis_uuid = analysis.get('uuid', '?')

            if self.api.claim_analysis(analysis_id):
                # Successfully claimed - process it
                log.info(f"Claimed analysis {analysis_id} ({analysis_uuid})")
                self.process_analysis(analysis)
                return 1
            else:
                # Already claimed by another worker, try next
                log.debug(f"Analysis {analysis_id} ({analysis_uuid}) already claimed, trying next")
                continue

        return 0

    def _install_signal_handlers(self):
        """Register SIGTERM/SIGINT handlers for graceful shutdown.

        Docker sends SIGTERM on ``docker compose stop``/``down``.  Because the
        worker runs as PID 1 in its container, Python's default signal
        disposition does not apply and an unhandled SIGTERM is ignored — the
        container then sits until the stop grace period elapses and is killed
        with SIGKILL (exit 137).  Installing an explicit handler lets us finish
        the in-flight job and exit cleanly.

        Must be called from the main thread (Python only allows signal handlers
        to be installed there); ``run()`` is invoked from ``main()`` so this
        holds.
        """
        def _handle(signum, _frame):
            name = signal.Signals(signum).name
            if self._shutdown.is_set():
                # Second signal: operator is impatient.  Honour it immediately
                # rather than waiting for the current job to finish.
                log.warning("Received %s again — exiting now", name)
                sys.exit(0)
            log.info(
                "Received %s — will finish the current job (if any) then shut down. "
                "Send the signal again to exit immediately.",
                name,
            )
            self._shutdown.set()

        for _sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(_sig, _handle)

    def _idle_sleep(self, delay: float):
        """Sleep up to ``delay`` seconds, waking early if shutdown is requested.

        ``time.sleep`` would resume after the signal handler returns (PEP 475),
        leaving the worker unresponsive to SIGTERM for up to a full backoff
        interval; waiting on the shutdown Event wakes immediately instead.
        """
        self._shutdown.wait(timeout=delay)

    def run(self):
        """Main worker loop."""
        self._install_signal_handlers()
        log.info("Starting Arcology worker")
        log.info(f"API: {self.api.api}")
        log.info(f"Uploads: {self.uploads}")
        log.info(f"Outputs: {self.outputs}")
        if self.analysis_types:
            log.info(f"Job type filter: {', '.join(self.analysis_types)}")
            from arcology_shared.enums import AnalysisType
            valid_names = set(AnalysisType.__members__)
            unknown = [t for t in self.analysis_types if t not in valid_names]
            if unknown:
                log.error(
                    "WORKER_ANALYSIS_TYPES contains unknown AnalysisType name(s): %s — "
                    "check spelling against arcology_shared/enums.py",
                    ', '.join(unknown),
                )
                sys.exit(1)
        else:
            log.info("Job type filter: all types")
        # Control-plane / DB-only jobs are owned by the taskrunner, never claimed
        # here (see _effective_types).
        log.info("Excluding control-plane job types (owned by taskrunner): %s",
                 ', '.join(sorted(self._control_plane_names)))

        # Recover any jobs left in RUNNING state by a previous worker crash
        self.api.reset_stale_analyses()
        last_stale_reset = time.monotonic()

        current_delay = POLL_BACKOFF_FLOOR

        while not self._shutdown.is_set():
            try:
                # Periodically re-queue jobs orphaned mid-run (e.g. a worker
                # SIGKILL'd past the stop grace period leaves its job stuck in
                # RUNNING). The startup reset above only catches jobs that were
                # already stale at boot; without this a stranded job would wait
                # for the next restart. The server applies STALE_JOB_TIMEOUT so
                # this never disturbs a job still running on a live worker.
                if STALE_RESET_INTERVAL > 0 and (
                        time.monotonic() - last_stale_reset >= STALE_RESET_INTERVAL):
                    self.api.reset_stale_analyses()
                    last_stale_reset = time.monotonic()

                processed = self.claim_and_process()

                # A shutdown signal may have arrived while the job ran; check
                # before sleeping or claiming another job.
                if self._shutdown.is_set():
                    break

                if processed == 0:
                    log.debug(f"No pending analyses, sleeping {current_delay}s")
                    self._idle_sleep(current_delay)
                    current_delay = min(current_delay * 2, POLL_BACKOFF_CEILING)
                else:
                    log.info(f"Processed {processed} analyses")
                    current_delay = POLL_BACKOFF_FLOOR

            except KeyboardInterrupt:
                log.info("Shutting down")
                break

            except Exception:
                log.exception("Unexpected error in main loop")
                self._idle_sleep(POLL_BACKOFF_CEILING)

        log.info("Worker shut down cleanly")
# vim: ts=4 sw=4 et
