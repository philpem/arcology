"""
Analysis worker and job processing.

Contains the main AnalysisWorker class that polls for jobs and
dispatches them to handlers.  Each handler lives in
``worker.arcworker.analyses`` and is bound to AnalysisWorker as a
class attribute at the bottom of this module so the function bodies
(which already reference ``self``) work unchanged.
"""

import shutil
import tempfile
import time
from pathlib import Path

from .config import log, MAX_POLL
from shared.enums import ArtefactType
from .compression import decompress_if_needed
from .api import ArcologyAPI
from .utils.text import make_latin1_fspath
from . import analyses as _analyses


_COMPRESS_SUFFIXES = frozenset({'.gz', '.bz2', '.zst'})


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

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path,
                 api_key: str = '', storage=None):
        """
        Initialize the worker.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded artefacts are stored
            output_dir: Directory for analysis outputs (e.g., visualisations)
            api_key: Worker API key for authentication
            storage: StorageBackend instance (if None, creates from config)
        """
        self.uploads = upload_dir
        self.outputs = output_dir
        self.outputs.mkdir(parents=True, exist_ok=True)

        if storage is None:
            from shared.storage import create_storage
            from .config import (STORAGE_BACKEND, S3_ENDPOINT_URL, S3_BUCKET,
                                 S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION,
                                 S3_PUBLIC_URL)
            storage = create_storage({
                'STORAGE_BACKEND': STORAGE_BACKEND,
                'S3_ENDPOINT_URL': S3_ENDPOINT_URL,
                'S3_BUCKET': S3_BUCKET,
                'S3_ACCESS_KEY': S3_ACCESS_KEY,
                'S3_SECRET_KEY': S3_SECRET_KEY,
                'S3_REGION': S3_REGION,
                'S3_PUBLIC_URL': S3_PUBLIC_URL,
                'UPLOAD_FOLDER': str(upload_dir),
                'OUTPUT_FOLDER': str(output_dir),
            })
        self.storage = storage

        self.api = ArcologyAPI(api_url, upload_dir, output_dir,
                               api_key=api_key, storage=storage)
        self._decompression_info = None  # Set by get_input_path() when decompression occurs

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

        from shared.storage import LocalStorage
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
            status='failed',
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
            from shared.storage import S3Storage
            if isinstance(self.storage, S3Storage):
                try:
                    rel = local_path.relative_to(self.outputs)
                    cache_key = self.storage.storage_key('outputs', str(rel))
                except ValueError:
                    cache_key = self.storage.storage_key(
                        'outputs', f".cache/{artefact['uuid']}/{local_path.name}")
                dest = work_dir / local_path.name
                try:
                    self.storage.get(cache_key, dest)
                    log.info(f"Downloaded cached partition image from storage: {cache_key}")
                    return dest
                except FileNotFoundError:
                    pass  # not cached yet; fall through to get_input_path
        return self.get_input_path(artefact, work_dir)

    def complete_analysis(self, analysis_id: int, summary: str | None = None, **kwargs):
        """Report a completed successful analysis to the API."""
        payload = {
            'status': 'completed',
            'success': True,
            **kwargs,
        }
        if summary is not None:
            payload['summary'] = summary
        self.api.update_analysis(analysis_id, **payload)

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
        from shared.enums import AnalysisType
        hints = {
            'filesystem': filesystem,
            'partition_index': partition_index,
        }
        if partition_image_path:
            hints['partition_image_path'] = partition_image_path
        if container_format:
            hints['container_format'] = container_format
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
        from shared.enums import AnalysisType
        archive_hints = {'partition_uuid': partition_uuid}
        if extraction_path:
            archive_hints['extraction_path'] = extraction_path
        if path_prefix:
            archive_hints['path_prefix'] = path_prefix
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.ARCHIVE_DETECT.value,
            hints=archive_hints,
        )
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.PRODUCT_RECOGNITION.value,
            hints={'partition_uuid': partition_uuid},
        )
        if extraction_path:
            self.api.queue_analysis(
                artefact_uuid,
                AnalysisType.FORMAT_CONVERT.value,
                hints={
                    'extraction_path': extraction_path,
                    'partition_uuid': partition_uuid,
                },
            )
        # RISC OS module metadata extraction — harmless no-op on non-Acorn
        # extractions since only filetype ffa files are scanned.
        module_hints = {'partition_uuid': partition_uuid}
        if extraction_path:
            module_hints['extraction_path'] = extraction_path
        self.api.queue_analysis(
            artefact_uuid,
            AnalysisType.RISCOS_MODULE_PARSE.value,
            hints=module_hints,
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
        from shared.storage import LocalStorage
        if isinstance(self.storage, LocalStorage):
            return
        rel = self._relative_output_path(extract_dir)
        prefix = self.storage.storage_key('outputs', rel)
        # Upload first; only remove the local copy on success.  If put_tree()
        # raises, the exception propagates (the analysis handler marks the job
        # failed), and the local directory is left in place so a retry can
        # re-upload without orphaning partially-uploaded S3 objects.
        count = self.storage.put_tree(prefix, extract_dir)
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
        from shared.storage import LocalStorage

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
        from shared.storage import LocalStorage
        if isinstance(self.storage, LocalStorage):
            return

        cache_dir = self.outputs / '.cache' / artefact_uuid
        if not cache_dir.exists():
            return

        # Ask the API whether any analyses are still pending or running
        try:
            resp = self.api.get(f'/artefacts/{artefact_uuid}/analysis')
            if resp and 'analyses' in resp:
                active = [
                    a for a in resp['analyses']
                    if a.get('status') in ('pending', 'running')
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
    # The handler functions live in ``worker.arcworker.analyses.*`` so each
    # category (flux, extraction, images, metadata, partition, armlock) has
    # its own module.  They are bound here as class attributes — Python turns
    # plain functions assigned to a class into bound methods on access, so
    # the bodies (which already reference ``self``) work unchanged.
    #
    # Static helpers (``_sniff_archive_magic``, ``_is_riscos_zip``) are wrapped
    # in ``staticmethod`` so callsites like ``self._sniff_archive_magic(p)``
    # don't try to pass ``self`` as the first argument.

    # Flux pipeline
    process_flux_visualisation    = _analyses.process_flux_visualisation
    process_detect_track_density  = _analyses.process_detect_track_density
    process_flux_decode           = _analyses.process_flux_decode
    process_disc_mastering_detect = _analyses.process_disc_mastering_detect
    process_disc_protection_detect = _analyses.process_disc_protection_detect

    # File / archive extraction
    process_file_extraction       = _analyses.process_file_extraction
    process_archive_detect        = _analyses.process_archive_detect
    process_archive_extract       = _analyses.process_archive_extract
    _extract_top_level_archive    = _analyses._extract_top_level_archive
    _sniff_archive_magic          = staticmethod(_analyses._sniff_archive_magic)
    _is_riscos_zip                = staticmethod(_analyses._is_riscos_zip)
    _PROMOTABLE_EXTENSIONS        = _analyses._PROMOTABLE_EXTENSIONS

    # Format conversion (Sprite / Draw / Text / images)
    process_format_convert        = _analyses.process_format_convert
    _convert_file_to_outputs      = _analyses._convert_file_to_outputs
    _detect_viewable_type         = _analyses._detect_viewable_type
    _RISCOS_VIEWABLE_SUFFIXES     = _analyses._RISCOS_VIEWABLE_SUFFIXES
    _EXT_VIEWABLE                 = _analyses._EXT_VIEWABLE
    _RISCOS_HEX_VIEWABLE          = _analyses._RISCOS_HEX_VIEWABLE

    # Metadata-style handlers
    process_checksum_compute      = _analyses.process_checksum_compute
    process_metadata_extract      = _analyses.process_metadata_extract
    process_format_identify       = _analyses.process_format_identify
    process_product_recognition   = _analyses.process_product_recognition
    process_riscos_module_parse   = _analyses.process_riscos_module_parse

    # Partition / armlock
    process_partition_detect      = _analyses.process_partition_detect
    process_armlock_remove        = _analyses.process_armlock_remove

    # =========================================================================
    # Job Processing
    # =========================================================================

    def process_analysis(self, analysis: dict):
        """Process a single analysis job."""
        analysis_id = analysis['id']
        analysis_uuid = analysis.get('uuid', '?')
        analysis_type = analysis['analysis_type']
        artefact = analysis.get('artefact', {})

        log.info(f"Processing analysis {analysis_id} ({analysis_uuid}): {analysis_type} for {artefact.get('label', 'unknown')}")

        # Status is already RUNNING from the atomic claim in claim_and_process().

        # Create temporary work directory
        with tempfile.TemporaryDirectory(prefix=f'arcology_{analysis_id}_') as work_dir:
            work_path = Path(work_dir)

            try:
                # Dispatch to appropriate handler
                handler_name = _analyses.HANDLERS.get(analysis_type)
                handler = getattr(self, handler_name, None) if handler_name else None
                if handler:
                    handler(analysis, artefact, work_path)
                else:
                    log.warning(f"Unknown analysis type: {analysis_type}")
                    self.fail_analysis(analysis_id, f'Unknown analysis type: {analysis_type}')

            except Exception as e:
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

    def claim_and_process(self) -> int:
        """
        Atomically claim a pending analysis and process it.

        This is safe for multiple workers - each worker claims one job at a time
        by setting status to 'running' before processing. The API ensures only
        one worker can claim each job.

        Returns:
            Number of analyses processed (0 or 1)
        """
        analyses = self.api.get_pending_analyses()

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

    def run(self):
        """Main worker loop."""
        log.info("Starting Arcology worker")
        log.info(f"API: {self.api.api}")
        log.info(f"Uploads: {self.uploads}")
        log.info(f"Outputs: {self.outputs}")

        # Recover any jobs left in RUNNING state by a previous worker crash
        self.api.reset_stale_analyses()

        MIN_POLL = 0.5
        current_delay = MIN_POLL

        while True:
            try:
                processed = self.claim_and_process()

                if processed == 0:
                    log.debug(f"No pending analyses, sleeping {current_delay}s")
                    time.sleep(current_delay)
                    current_delay = min(current_delay * 2, MAX_POLL)
                else:
                    log.info(f"Processed {processed} analyses")
                    current_delay = MIN_POLL

            except KeyboardInterrupt:
                log.info("Shutting down")
                break

            except Exception as e:
                log.exception("Unexpected error in main loop")
                time.sleep(MAX_POLL)
# vim: ts=4 sw=4 et
