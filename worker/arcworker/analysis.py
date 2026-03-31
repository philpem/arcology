"""
Analysis worker and job processing.

Contains the main AnalysisWorker class that polls for jobs and
dispatches them to appropriate handlers.
"""

import functools
import hashlib
import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from .config import log, MAX_POLL, MASTERING_TRACK_SCAN_COUNT
from shared.enums import ArtefactType, AnalysisType
from .compression import decompress_if_needed, extract_partition_range, is_region_uniform
from .api import ArcologyAPI
from .utils.text import make_latin1_fspath, sanitize_path
from .tools import (
    compute_file_hash,
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    flux_to_imd_hxcfe,
    flux_to_hfe_hxcfe,
    sector_image_to_raw_greaseweazle,
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    enumerate_extracted_files,
    parse_acorn_filename,
    detect_partitions_sfdisk,
    detect_acorn_adfs,
    detect_acorn_partitions,
    detect_format_file_cmd,
    detect_fat_filesystem,
    detect_armlock,
    remove_armlock,
    convert_sprite,
    convert_draw,
)


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
        def wrapper(self, analysis: dict, artefact: dict, work_dir: Path):
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


class AnalysisWorker:
    """Main worker class that processes analysis jobs."""

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path, api_key: str = ''):
        """
        Initialize the worker.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded artefacts are stored
            output_dir: Directory for analysis outputs (e.g., visualisations)
            api_key: Worker API key for authentication
        """
        self.uploads = upload_dir
        self.outputs = output_dir
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.api = ArcologyAPI(api_url, upload_dir, output_dir, api_key=api_key)
        self._decompression_info = None  # Set by get_input_path() when decompression occurs

    def get_input_path(self, artefact: dict, work_dir: Path) -> Path:
        """
        Get input file path, decompressing if needed.

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

        # Use uploads or outputs directory based on storage_directory field
        if storage_directory == 'outputs':
            input_path = self.outputs / storage_path
        else:
            input_path = self.uploads / storage_path

        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        result = decompress_if_needed(input_path, work_dir)

        # Track decompression metadata for handlers that need it
        if result != input_path:
            self._decompression_info = {
                'was_decompressed': True,
                'compressed_name': input_path.name,
                'compressed_size': input_path.stat().st_size,
                'decompressed_name': result.name,
                'decompressed_size': result.stat().st_size,
                'compression_format': input_path.suffix.lower(),
            }
        else:
            self._decompression_info = None

        return result

    def save_output_file(self, source_path: Path, filename: str, subdir: str | None = None) -> str:
        """
        Save an output file (like a visualisation) to the outputs directory.

        Args:
            source_path: Path to the generated file
            filename: Destination filename
            subdir: Optional subdirectory within outputs (e.g. '{item_uuid}_{item_slug}/{artefact_uuid}_{artefact_slug}')

        Returns:
            The relative path for use in URLs (subdir/filename or just filename)
        """
        if subdir:
            dest_dir = self.outputs / subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / filename
            relative_path = f"{subdir}/{filename}"
        else:
            dest_path = self.outputs / filename
            relative_path = filename
        shutil.copy(source_path, dest_path)
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
    ):
        """Queue FILE_EXTRACTION with the standard hint structure."""
        hints = {
            'filesystem': filesystem,
            'partition_index': partition_index,
        }
        if partition_image_path:
            hints['partition_image_path'] = partition_image_path
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
                hints={'extraction_path': extraction_path},
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

    # =========================================================================
    # Analysis Handlers
    # =========================================================================

    @analysis_handler("flux visualisation")
    def process_flux_visualisation(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process FLUX_VISUALISATION analysis."""
        analysis_id = analysis['id']
        analysis_uuid = analysis['uuid']

        input_path = self.get_input_path(artefact, work_dir)

        # Build output subdirectory: {item_uuid}_{item_slug}/{artefact_uuid}_{artefact_slug}
        item = artefact.get('item', {})
        item_uuid = item.get('uuid', '')
        item_slug = item.get('slug', '')
        artefact_uuid = artefact.get('uuid', '')
        artefact_slug = artefact.get('slug', '')
        item_part = f"{item_uuid}_{item_slug}" if item_slug else item_uuid
        artefact_part = f"{artefact_uuid}_{artefact_slug}" if artefact_slug else artefact_uuid
        output_subdir = f"{item_part}/{artefact_part}" if (item_part and artefact_part) else None

        outputs = []

        # Try Fluxfox first (more detailed)
        # Use analysis UUID to prevent overwrites when re-running analysis
        output_fluxfox = work_dir / f"{analysis_uuid}_fluxfox.png"
        result_fluxfox = flux_visualisation_fluxfox(input_path, output_fluxfox)

        if result_fluxfox['success']:
            saved_name = self.save_output_file(output_fluxfox, f"{analysis_uuid}_fluxfox.png", subdir=output_subdir)
            outputs.append({
                'tool': 'fluxfox',
                'type': 'image',
                'filename': saved_name,
                'description': 'Fluxfox visualisation'
            })

        # Also generate HxCFE visualisation (different style)
        output_hxcfe = work_dir / f"{analysis_uuid}_hxcfe.png"
        result_hxcfe = flux_visualisation_hxcfe(input_path, output_hxcfe)

        if result_hxcfe['success']:
            saved_name = self.save_output_file(output_hxcfe, f"{analysis_uuid}_hxcfe.png", subdir=output_subdir)
            outputs.append({
                'tool': 'hxcfe',
                'type': 'image',
                'filename': saved_name,
                'description': 'HxCFE visualisation'
            })

        if outputs:
            self.complete_analysis(
                analysis_id,
                tool_name='fluxfox,hxcfe',
                summary=f'Generated {len(outputs)} flux visualisation(s)',
                details=json.dumps({
                    'outputs': outputs,
                    'fluxfox': result_fluxfox,
                    'hxcfe': result_hxcfe
                })
            )
        else:
            self.fail_analysis(
                analysis_id,
                f"Fluxfox: {result_fluxfox.get('error', 'unknown')}; HxCFE: {result_hxcfe.get('error', 'unknown')}"
            )

    @analysis_handler("flux decode")
    def process_flux_decode(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FLUX_DECODE analysis.
        Attempts to decode flux to sector image, producing derived artefacts.
        """
        analysis_id = analysis['id']
        results = []

        input_path = self.get_input_path(artefact, work_dir)
        artefact_label = artefact['label']

        # 1. Convert to IMD (preserves track metadata)
        imd_path = work_dir / f"{input_path.stem}.imd"
        imd_result = flux_to_imd_hxcfe(input_path, imd_path)
        results.append(('IMD', imd_result))

        if imd_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (IMD)",
                imd_path,
                ArtefactType.IMD
            )
            log.info(f"Created derived IMD artefact: {derived}")

        # 2. Convert to HFE (for emulators)
        hfe_path = work_dir / f"{input_path.stem}.hfe"
        hfe_result = flux_to_hfe_hxcfe(input_path, hfe_path)
        results.append(('HFE', hfe_result))

        if hfe_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (HFE)",
                hfe_path,
                ArtefactType.HFE
            )
            log.info(f"Created derived HFE artefact: {derived}")

        # 3. Convert to raw IMG via Greaseweazle (best for file extraction)
        # Use the IMD as input if available, otherwise try direct
        if imd_result['success']:
            img_input = imd_path
        else:
            img_input = input_path

        img_path = work_dir / f"{input_path.stem}.img"
        img_result = sector_image_to_raw_greaseweazle(img_input, img_path)
        results.append(('IMG', img_result))

        if img_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (raw sectors)",
                img_path,
                ArtefactType.RAW_SECTOR
            )
            log.info(f"Created derived IMG artefact: {derived}")

        # Report results
        any_success = any(r[1]['success'] for r in results)
        summary_parts = [f"{name}: {'OK' if r['success'] else 'FAIL'}" for name, r in results]

        if any_success:
            self.complete_analysis(
                analysis_id,
                tool_name='hxcfe,greaseweazle',
                summary='; '.join(summary_parts),
                details=json.dumps({name: r for name, r in results})
            )
        else:
            self.fail_analysis(
                analysis_id,
                '; '.join(summary_parts),
                tool_name='hxcfe,greaseweazle',
                details=json.dumps({name: r for name, r in results})
            )

    @analysis_handler("file extraction")
    def process_file_extraction(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_EXTRACTION analysis.
        Extracts files from a disc/sector image to persistent storage,
        registers the file listing in the database, and queues archive detection.
        Only works on raw sector images (IMG) - not HFE or IMD formats.

        When a 'partition_image_path' hint is provided (set by PARTITION_DETECT),
        uses that file directly instead of re-decompressing the original artefact.
        """
        from .tools.extraction import _parse_dim_report
        from .utils.paths import get_output_path
        from .config import OUTPUT_DIR

        analysis_id = analysis['id']
        artefact_type = artefact.get('artefact_type', '')

        # Only raw sector images can be processed by 7z and DIM
        # HFE is an emulator container format, IMD is track-based with metadata
        # These need to be converted to IMG first via flux_decode
        supported_types = (
            ArtefactType.ISO.value,
            ArtefactType.RAW_SECTOR.value,
            ArtefactType.DD_ZST.value,
            ArtefactType.DD_GZ.value,
            ArtefactType.DD_BZ2.value,
        )
        if artefact_type not in supported_types:
            self.fail_analysis(
                analysis_id,
                f'File extraction not supported for {artefact_type} format. Only raw sector images are supported.',
                details=json.dumps({
                    'artefact_type': artefact_type,
                    'supported_types': list(supported_types),
                })
            )
            return

        hints = json.loads(analysis.get('hints') or '{}')
        filesystem = hints.get('filesystem', '').lower()
        partition_index = hints.get('partition_index', 0)

        # Use cached partition image from PARTITION_DETECT when available,
        # avoiding redundant decompression of the original artefact.
        partition_image_path = hints.get('partition_image_path')
        if partition_image_path and Path(partition_image_path).exists():
            input_path = Path(partition_image_path)
            log.info(f"Using cached partition image: {input_path}")
        else:
            input_path = self.get_input_path(artefact, work_dir)

        # Get Item for hierarchical path
        item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

        # Use hierarchical output path for persistent storage
        extract_dir = get_output_path(
            OUTPUT_DIR,
            item,
            artefact,
            analysis,
            partition=None
        )

        # Track every tool attempted so all process_output ends up in details.
        all_results: dict[str, dict] = {}

        # Choose extraction method based on filesystem hint
        is_acorn = False
        if filesystem in ('dfs', 'adfs', 'acorn'):
            result = extract_acorn_disc_image_manager(input_path, extract_dir)
            all_results['dim'] = result
            is_acorn = True
        elif filesystem in ('fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos'):
            result = extract_dos_7z(input_path, extract_dir)
            all_results['7z'] = result
        else:
            # No filesystem hint — read the boot-sector BPB first.  If the
            # image is FAT12/16/32, skip DIM entirely and go straight to 7z:
            # DIM can read DOS FAT images but produces double-extension names.
            # We must NOT try 7z first without this check because 7z will
            # "succeed" on ADFS images containing ZIP files by extracting the
            # embedded ZIP rather than the actual disc filesystem.
            if detect_fat_filesystem(input_path):
                result = extract_dos_7z(input_path, extract_dir)
                all_results['7z'] = result
            else:
                result = extract_acorn_disc_image_manager(input_path, extract_dir)
                all_results['dim'] = result
                if result['success']:
                    is_acorn = True
                else:
                    result = extract_dos_7z(input_path, extract_dir)
                    all_results['7z'] = result

        def _build_details(extra: dict | None = None) -> str:
            d: dict = {}
            if extra:
                d.update(extra)
            for tool_key, tool_result in all_results.items():
                po = tool_result.get('process_output')
                if po:
                    d[tool_key] = {'process_output': po}
            return json.dumps(d)

        if not result['success']:
            self.fail_analysis(
                analysis_id,
                result.get('error', 'Extraction failed'),
                tool_name=result.get('tool'),
                details=_build_details()
            )
            return

        # Enumerate extracted files to build file listing
        files = enumerate_extracted_files(extract_dir, acorn=is_acorn)

        # Extract disc metadata from DIM report output (if Acorn)
        disc_name = None
        container_format = None
        if is_acorn and result.get('process_output'):
            metadata = _parse_dim_report(result['process_output'].get('stdout', ''))
            disc_name = metadata.get('disc_name')
            container_format = metadata.get('container_format')

        # Determine filesystem type
        if filesystem:
            fs_type = filesystem
        elif container_format:
            container_lower = container_format.lower()
            if 'adfs' in container_lower:
                fs_type = 'adfs'
            elif 'dfs' in container_lower:
                fs_type = 'dfs'
            elif 'acorn' in container_lower:
                fs_type = 'acorn'
            else:
                fs_type = 'unknown'
        else:
            fs_type = 'unknown'

        # For DOS/FAT filesystems processed by 7z, construct a human-readable
        # container_format so the UI hover tooltip is populated.  DIM sets this
        # automatically for Acorn images; for DOS images DIM is never used.
        if not container_format:
            _fat_labels = {
                'fat12': 'DOS FAT12',
                'fat16': 'DOS FAT16',
                'fat32': 'DOS FAT32',
                'fat':   'DOS FAT',
                'dos':   'DOS',
                'msdos': 'MS-DOS',
            }
            container_format = _fat_labels.get(fs_type)

        # Register partition and file listing in the database
        partition = self.api.register_file_listing(
            artefact['uuid'],
            files,
            fs_type,
            label=disc_name,
            container_format=container_format,
            partition_index=partition_index,
        )

        self.complete_analysis(
            analysis_id,
            tool_name=result['tool'],
            summary=f'Extracted {len(files)} files ({fs_type})',
            output_path=str(extract_dir),
            details=_build_details({'file_count': len(files)})
        )

        # Queue ARCHIVE_DETECT to scan extracted files for nested archives
        if partition:
            self.queue_partition_follow_ups(
                artefact['uuid'],
                partition.get('uuid'),
                extraction_path=str(extract_dir),
            )

    @analysis_handler("checksum computation")
    def process_checksum_compute(self, analysis: dict, artefact: dict, work_dir: Path):
        """Compute MD5 and SHA256 hashes for the artefact file and store them."""
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)

        md5, sha256, size = compute_file_hash(input_path)
        self.api.update_artefact_hashes(artefact['uuid'], md5, sha256)

        self.complete_analysis(
            analysis_id,
            summary=f'MD5: {md5}  SHA256: {sha256}',
            details=json.dumps({'md5': md5, 'sha256': sha256, 'size': size}),
        )

    @analysis_handler("metadata extraction")
    def process_metadata_extract(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process METADATA_EXTRACT analysis.
        Extracts format-specific metadata.
        """
        analysis_id = analysis['id']

        input_path = self.get_input_path(artefact, work_dir)
        artefact_type = artefact['artefact_type']

        metadata = {}

        # Get basic file info
        md5, sha256, size = compute_file_hash(input_path)
        metadata['file'] = {
            'size': size,
            'md5': md5,
            'sha256': sha256
        }

        self.complete_analysis(
            analysis_id,
            summary=f'Extracted metadata for {artefact_type}',
            details=json.dumps(metadata)
        )

    @analysis_handler("format identification")
    def process_format_identify(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FORMAT_IDENTIFY analysis.
        Attempts to identify the exact format of an image.

        Currently handles:
        - FCFS hard disk images (FileCore Filing System, RISC OS filetype &FCD).
          Detected via the 4-byte magic "FCFS" at file_size - 256.
          Converts to a raw sector image with fcfs2raw, then registers the
          result as a derived RAW_SECTOR artefact so that PARTITION_DETECT
          and FILE_EXTRACTION run automatically.
        """
        from .tools.extraction import convert_fcfs_to_raw

        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        artefact_label = artefact.get('label', 'image')

        # -------------------------------------------------------------------
        # FCFS detection: 4-byte magic "FCFS" at offset file_size - 256.
        # This is the standard FCFS trailer location documented in fcfs2raw.c.
        # -------------------------------------------------------------------
        detected_format = 'unknown'
        file_size = input_path.stat().st_size
        if file_size >= 256:
            try:
                with open(input_path, 'rb') as f:
                    f.seek(file_size - 256)
                    magic = f.read(4)
                if magic == b'FCFS':
                    detected_format = 'fcfs'
            except OSError:
                pass

        if detected_format == 'fcfs':
            raw_path = work_dir / 'converted.img'
            conv_result = convert_fcfs_to_raw(input_path, raw_path)

            if not conv_result['success']:
                self.fail_analysis(
                    analysis_id,
                    f'FCFS detected but conversion failed: {conv_result.get("error", "unknown")}',
                    tool_name='fcfs2raw',
                    details=json.dumps({'detected': 'fcfs', 'fcfs2raw': conv_result})
                )
                return

            # Register derived RAW_SECTOR artefact.  auto_analyse=True causes
            # the web app to queue PARTITION_DETECT automatically, which in
            # turn queues FILE_EXTRACTION once partitions are mapped.
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (raw sectors)",
                raw_path,
                ArtefactType.RAW_SECTOR,
                auto_analyse=True
            )

            self.complete_analysis(
                analysis_id,
                tool_name='fcfs2raw',
                summary='Identified as FCFS hard disk image; converted to raw sectors',
                details=json.dumps({
                    'detected': 'fcfs',
                    'fcfs2raw': conv_result,
                    'derived_artefact': derived,
                })
            )
            return

        # No format recognised yet.
        self.complete_analysis(
            analysis_id,
            summary='Format not identified',
            details=json.dumps({'detected': 'unknown'})
        )

    @analysis_handler("partition detection")
    def process_partition_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process PARTITION_DETECT analysis.
        Detects partitions and filesystem types in raw disc images.

        Detection order:
        1. Acorn partition schemes (Nexus, HCCS, Simtec, etc.) — checked
           first because they have specific magic bytes and don't depend on
           ADFS boot block checksums.  Nexus is tried before HCCS because
           on Nexus discs the area at 0xC00 holds sharer firmware (not an
           HCCS boot block).  A PC disc reformatted as Acorn may retain a
           stale MBR, so sfdisk must come after.
        2. ADFS filesystem signatures — for unpartitioned Acorn discs.
        3. sfdisk — standard MBR/GPT partition tables.

        Fully-decoded schemes (Nexus, HCCS) provide per-partition metadata
        including disc names and access information.  Signature-only
        schemes (Simtec) are noted but fall through to whole-disc handling.

        All partition schemes normalise to byte-based offsets (start_byte /
        size_bytes) so the carving and gap-detection code below is
        addressing-mode agnostic.

        When multiple partitions are detected (or a single partition that
        doesn't span the whole disc), each partition is extracted and
        registered as a derived artefact with FILE_EXTRACTION queued
        against it.  Any unpartitioned gaps between/around partitions are
        also preserved as derived artefacts unless the gap is uniform
        (every byte the same value), in which case it is omitted with a
        note in the summary.

        For single-partition whole-disc images (ADFS, no partition table),
        FILE_EXTRACTION is queued against the original artefact.  When the
        original file was compressed, a decompressed copy is cached so
        that FILE_EXTRACTION doesn't have to decompress again.
        """
        # Common MBR partition type codes -> filesystem hints
        _MBR_TYPE_TO_FS = {
            '1': 'fat12', '4': 'fat16', '6': 'fat16',
            'b': 'fat32', 'c': 'fat32', 'e': 'fat16',
            '7': 'ntfs',
            '11': 'fat32', '14': 'fat16',
        }

        analysis_id = analysis['id']

        input_path = self.get_input_path(artefact, work_dir)
        decompression_info = self._decompression_info  # Capture before it's overwritten
        hints = json.loads(analysis.get('hints') or '{}')
        filesystem_hint = hints.get('filesystem', '').lower()

        results = {}
        detected_partitions = []

        # 1. Try Acorn partition schemes (HCCS, Simtec, etc.) first.
        # These check for specific magic bytes at known boot block offsets
        # and must run before sfdisk, which would report stale MBR partition
        # tables left behind when a PC disc is reformatted as Acorn.
        # This does NOT depend on ADFS signature detection — the boot block
        # checksum can fail even on a valid HCCS disc (e.g. if the checksum
        # byte is missing or the first sector contains a stale MBR).
        acorn_result = detect_acorn_partitions(input_path)
        results['acorn_partitions'] = acorn_result

        if acorn_result['detected'] and acorn_result.get('partitions'):
            # Fully decoded scheme (e.g. HCCS) with partition list
            detected_partitions = acorn_result['partitions']
        elif acorn_result['detected']:
            # Signature-only scheme (e.g. Simtec) — no decoded partitions.
            # Treat as single ADFS partition spanning the whole disc.
            detected_partitions = [{
                'index': 0,
                'start_byte': 0,
                'filesystem': 'adfs',
                'description': acorn_result.get('description', ''),
                'size_bytes': input_path.stat().st_size,
            }]

        # 2. Check for ADFS filesystem signatures.
        # Always run for metadata (signatures are useful in the summary),
        # but only use for partitioning if no Acorn scheme was found above.
        adfs_result = detect_acorn_adfs(input_path)
        results['adfs'] = adfs_result

        if not detected_partitions and adfs_result.get('adfs_detected'):
            detected_partitions = [{
                'index': 0,
                'start_byte': 0,
                'filesystem': 'adfs',
                'description': f'Acorn ADFS ({adfs_result.get("adfs_variant", "unknown variant")})',
                'size_bytes': input_path.stat().st_size,
                'signatures': adfs_result.get('signatures', []),
            }]

        # 3. If no Acorn formats, try sfdisk for standard partition tables (MBR/GPT)
        if not detected_partitions:
            sfdisk_result = detect_partitions_sfdisk(input_path)
            results['sfdisk'] = sfdisk_result

            if sfdisk_result['success']:
                detected_partitions = sfdisk_result['partitions']
                # Enrich sfdisk partitions with filesystem hints from type codes
                for p in detected_partitions:
                    ptype = p.get('type', '').lower()
                    p['filesystem'] = _MBR_TYPE_TO_FS.get(ptype, 'unknown')

        # 4. Use file command for additional format info
        file_result = detect_format_file_cmd(input_path)
        results['file'] = file_result

        # 5. If nothing detected, report whole disc as single unknown partition.
        # Use boot-sector BPB parsing to identify FAT12/16/32 before falling
        # back to 'unknown'.  The 'file' command output is intentionally not
        # used for this decision because its output format is not stable enough
        # for machine parsing.
        if not detected_partitions:
            file_size = input_path.stat().st_size
            inferred_fs = detect_fat_filesystem(input_path) or 'unknown'
            detected_partitions = [{
                'index': 0,
                'start_byte': 0,
                'filesystem': filesystem_hint or inferred_fs,
                'description': 'No partition table detected (whole disc)',
                'size_bytes': file_size,
            }]

        # -----------------------------------------------------------------
        # Decide how to persist partition images for FILE_EXTRACTION
        # -----------------------------------------------------------------
        disc_size = input_path.stat().st_size
        is_compressed = decompression_info is not None

        # Register partitions as derived artefacts when there are multiple
        # partitions, or a single partition that doesn't span the whole disc.
        # This also lets us extract and preserve unpartitioned space (gaps).
        # All partition schemes normalise to start_byte / size_bytes so the
        # logic below is addressing-mode agnostic.
        should_register_derived = False
        if len(detected_partitions) > 1:
            should_register_derived = True
        elif len(detected_partitions) == 1:
            p = detected_partitions[0]
            p_start = p.get('start_byte', 0)
            p_end = p_start + p.get('size_bytes', disc_size)
            if p_start > 0 or p_end < disc_size:
                should_register_derived = True

        unpartitioned_notes: list[str] = []
        partition_image_paths: dict[int, str] = {}
        artefact_label = artefact.get('label', 'Unknown')

        if should_register_derived:
            # ---- Extract each partition as a derived artefact ----
            for partition in detected_partitions:
                idx = partition['index']
                fs = partition.get('filesystem', 'unknown')
                start_byte = partition.get('start_byte', 0)
                size_bytes = partition.get('size_bytes', 0)

                # Build a descriptive label including disc name when available
                disc_name = partition.get('disc_name')
                if disc_name:
                    part_label = f"{artefact_label} (partition {idx}: {disc_name}, {fs})"
                else:
                    part_label = f"{artefact_label} (partition {idx}, {fs})"

                partition_path = work_dir / f"partition_{idx}.img"
                extract_partition_range(input_path, partition_path, start_byte, size_bytes)

                derived = self.api.register_derived_artefact(
                    analysis_id,
                    part_label,
                    partition_path,
                    ArtefactType.RAW_SECTOR,
                    auto_analyse=False
                )
                if derived:
                    derived_uuid = derived['artefact']['uuid']
                    # Nexus printer partitions are not Filecore formatted; skip
                    # FILE_EXTRACTION so they are only registered as downloadable
                    # raw artefacts (see issue #89).
                    is_nexus_printer = partition.get('nexus_flags', {}).get('printer', False)
                    if not is_nexus_printer:
                        if fs == 'adfs':
                            # Check for ARMlock protection on the extracted partition
                            # image before deciding what to queue next.  Detection is
                            # pure-Python and fast (no subprocess).  Only queue the
                            # removal step when protection is actually present; otherwise
                            # proceed directly to FILE_EXTRACTION as for any other fs.
                            armlock = detect_armlock(partition_path)
                            if armlock.get('detected'):
                                self.api.queue_analysis(
                                    derived_uuid,
                                    AnalysisType.ARMLOCK_REMOVE.value,
                                    hints={'filesystem': fs, 'partition_index': idx}
                                )
                            else:
                                self.queue_file_extraction(derived_uuid, fs, idx)
                        else:
                            self.queue_file_extraction(derived_uuid, fs, idx)
                else:
                    log.error(f"Failed to register partition {idx} as derived artefact")

            # ---- Handle unpartitioned space (gaps) ----
            sorted_parts = sorted(
                detected_partitions,
                key=lambda p: p.get('start_byte', 0)
            )

            gaps: list[dict] = []
            # Space before first partition.
            # On Nexus discs this region contains the disc sharer firmware.
            first_start = sorted_parts[0].get('start_byte', 0)
            if first_start > 0:
                if acorn_result.get('scheme') == 'nexus':
                    pre_label = 'Nexus firmware'
                else:
                    pre_label = 'Pre-partition space'
                gaps.append({'start': 0, 'size': first_start, 'label': pre_label})
            # Space between consecutive partitions
            for i in range(len(sorted_parts) - 1):
                end_curr = sorted_parts[i].get('start_byte', 0) + sorted_parts[i].get('size_bytes', 0)
                start_next = sorted_parts[i + 1].get('start_byte', 0)
                if start_next > end_curr:
                    gaps.append({
                        'start': end_curr, 'size': start_next - end_curr,
                        'label': f'Gap between partitions {sorted_parts[i]["index"]} and {sorted_parts[i + 1]["index"]}',
                    })
            # Space after last partition
            last_end = sorted_parts[-1].get('start_byte', 0) + sorted_parts[-1].get('size_bytes', 0)
            if last_end < disc_size:
                gaps.append({
                    'start': last_end, 'size': disc_size - last_end,
                    'label': 'Post-partition space',
                })

            for gap in gaps:
                uniform, fill_byte = is_region_uniform(input_path, gap['start'], gap['size'])
                if uniform:
                    note = (
                        f'{gap["label"]} ({gap["size"]:,} bytes) omitted: '
                        f'uniform fill 0x{fill_byte:02X}'
                    )
                    unpartitioned_notes.append(note)
                    log.info(f"Skipping {note}")
                else:
                    gap_path = work_dir / f"unpartitioned_{gap['start']:#x}.img"
                    extract_partition_range(input_path, gap_path, gap['start'], gap['size'])
                    self.api.register_derived_artefact(
                        analysis_id,
                        f"{artefact_label} ({gap['label']})",
                        gap_path,
                        ArtefactType.UNKNOWN,
                        auto_analyse=False
                    )
        else:
            # Single partition covering the whole disc (ADFS, no partition
            # table, or sfdisk single-partition spanning entire image).
            # For compressed files, cache the decompressed image so
            # FILE_EXTRACTION doesn't have to decompress again.
            if is_compressed:
                cache_dir = self.outputs / '.cache' / artefact['uuid']
                cache_dir.mkdir(parents=True, exist_ok=True)
                for partition in detected_partitions:
                    idx = partition['index']
                    partition_path = cache_dir / f"partition_{idx}.img"
                    shutil.copy(input_path, partition_path)
                    partition_image_paths[idx] = str(partition_path)
                    log.info(f"Cached decompressed image as {partition_path}")

            # Queue FILE_EXTRACTION (or ARMLOCK_REMOVE if protected) against
            # the original artefact.  For ADFS, run a quick inline Armlock check so
            # we only add the removal step when protection is actually present.
            for partition in detected_partitions:
                idx = partition['index']
                fs = partition.get('filesystem', 'unknown')
                next_hints: dict = {'filesystem': fs, 'partition_index': idx}
                if idx in partition_image_paths:
                    next_hints['partition_image_path'] = partition_image_paths[idx]
                if fs == 'adfs':
                    check_path = (
                        Path(partition_image_paths[idx])
                        if idx in partition_image_paths
                        else input_path
                    )
                    armlock = detect_armlock(check_path)
                    if armlock.get('detected'):
                        self.api.queue_analysis(
                            artefact['uuid'],
                            AnalysisType.ARMLOCK_REMOVE.value,
                            hints=next_hints,
                        )
                    else:
                        self.queue_file_extraction(
                            artefact['uuid'],
                            fs,
                            idx,
                            partition_image_path=next_hints.get('partition_image_path'),
                        )
                else:
                    self.queue_file_extraction(
                        artefact['uuid'],
                        fs,
                        idx,
                        partition_image_path=next_hints.get('partition_image_path'),
                    )

        # -----------------------------------------------------------------
        # Build summary and details
        # -----------------------------------------------------------------
        fs_types = [p.get('filesystem', 'unknown') for p in detected_partitions]
        summary = ''

        if decompression_info:
            summary += (
                f'Decompressed {decompression_info["compressed_name"]} \u2192 '
                f'{decompression_info["decompressed_name"]} '
                f'({decompression_info["decompressed_size"]:,} bytes). '
            )

        summary += f'Detected {len(detected_partitions)} partition(s): {", ".join(fs_types)}'

        # Acorn partition scheme info
        acorn_result = results.get('acorn_partitions', {})
        if acorn_result.get('detected'):
            scheme = acorn_result.get('scheme', 'unknown')
            if acorn_result.get('partitions'):
                summary += f' [{scheme.upper()} partitioning]'
                # Include disc names in summary
                names = [p.get('disc_name', '') for p in acorn_result['partitions'] if p.get('disc_name')]
                if names:
                    summary += f' (volumes: {", ".join(names)})'
            elif acorn_result.get('description'):
                summary += f'. {acorn_result["description"]}'

        if adfs_result.get('adfs_detected') and not acorn_result.get('detected'):
            summary += f' (ADFS signatures: {", ".join(adfs_result.get("signatures", []))})'

        if file_result.get('file_type'):
            summary += f' [file: {file_result["file_type"][:200]}]'

        if unpartitioned_notes:
            summary += '. ' + '; '.join(unpartitioned_notes)

        # Store each tool's result at the top level so the template's Process Logs
        # section can find process_output for sfdisk and file (adfs detection is
        # pure-Python and produces no subprocess output).
        details: dict = {'partitions': detected_partitions}
        details.update(results)
        if decompression_info:
            details['decompression'] = decompression_info
        if partition_image_paths:
            details['cached_partitions'] = partition_image_paths
        if unpartitioned_notes:
            details['unpartitioned_notes'] = unpartitioned_notes

        self.complete_analysis(
            analysis_id,
            tool_name='sfdisk,adfs_detect,file',
            summary=summary,
            details=json.dumps(details)
        )

    @analysis_handler("archive detection")
    def process_archive_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process ARCHIVE_DETECT analysis.
        Scans partition files for archives and queues extraction jobs.
        """
        import json
        from shared.archive_formats import (
            get_archive_by_filetype,
            get_archive_by_extension,
            get_archive_info,
            is_compressor_format,
        )
        from .config import MAX_ARCHIVE_DEPTH

        analysis_id = analysis['id']

        hints = json.loads(analysis.get('hints') or '{}')
        partition_uuid = hints.get('partition_uuid')
        extraction_path = hints.get('extraction_path')
        path_prefix = hints.get('path_prefix', '')

        if not partition_uuid:
            self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
            return

        # Get files not yet marked as archives (skip already-detected ones).
        # Must include known files (show_known=true) because archive files
        # can match the known-files database and would otherwise be hidden.
        partition_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000&is_archive=false&show_known=true")
        if not partition_resp:
            self.fail_analysis(analysis_id, 'Failed to get partition files')
            return

        files = partition_resp.get('files', [])

        # Filter files to only those belonging to this archive's extraction
        # context.  Without this, nested ARCHIVE_DETECT jobs pick up files
        # from unrelated archives in the same partition and pass them wrong
        # extraction_path / path_prefix hints, causing "file not found" in
        # the subsequent ARCHIVE_EXTRACT.
        if path_prefix:
            # Nested detection: only process files extracted from this archive
            # (their DB paths are prefixed with the archive's own path).
            files = [f for f in files if f.get('path', '').startswith(path_prefix + '/')]
        else:
            # Top-level detection (after FILE_EXTRACTION): only process files
            # that came directly from the disc image, not from nested archives.
            files = [f for f in files if f.get('extraction_depth', 0) == 0]

        archive_count = 0
        queued_count = 0
        depth_limit_exceeded = 0
        compressor_count = 0

        for file_data in files:
            filetype = file_data.get('risc_os_filetype')
            filename = file_data.get('filename', '')

            # Try detecting by RISC OS filetype first
            archive_type = get_archive_by_filetype(filetype) if filetype else None

            # Fall back to extension-based detection (for PC archives)
            if not archive_type:
                archive_type = get_archive_by_extension(filename)

            if not archive_type:
                continue

            archive_info = get_archive_info(archive_type)

            # Check if this is a single-file compressor
            is_compressor = is_compressor_format(archive_type)

            # Check depth limit
            current_depth = file_data.get('extraction_depth', 0)
            if current_depth >= MAX_ARCHIVE_DEPTH:
                depth_limit_exceeded += 1
                # Mark as archive but don't queue extraction
                self.api.post(f"/files/{file_data['id']}/mark_archive", {
                    'is_archive': True,
                    'archive_format': archive_info['name']
                })
                continue

            # Mark as archive
            self.api.post(f"/files/{file_data['id']}/mark_archive", {
                'is_archive': True,
                'archive_format': archive_info['name']
            })
            archive_count += 1
            if is_compressor:
                compressor_count += 1

            # Queue extraction
            extract_hints = {
                'file_id': file_data['id'],
                'partition_uuid': partition_uuid,
                'archive_type': archive_type.value,
                'archive_format': archive_info['name'],
                'is_compressor': is_compressor,
                'extraction_depth': current_depth + 1,
            }
            if extraction_path:
                extract_hints['extraction_path'] = extraction_path
            if path_prefix:
                extract_hints['path_prefix'] = path_prefix
            self.api.queue_analysis(
                artefact['uuid'],
                AnalysisType.ARCHIVE_EXTRACT.value,
                hints=extract_hints
            )
            queued_count += 1

        summary = f"Detected {archive_count} archives ({compressor_count} compressors), queued {queued_count} for extraction"
        if depth_limit_exceeded > 0:
            summary += f", {depth_limit_exceeded} at depth limit"

        self.complete_analysis(
            analysis_id,
            summary=summary,
            details=json.dumps({
                'archives_found': archive_count,
                'compressors_found': compressor_count,
                'depth_limit_exceeded': depth_limit_exceeded
            })
        )

    @staticmethod
    def _sniff_archive_magic(file_path: Path):
        """Sniff the first bytes of a file to detect mis-labelled archives.

        Returns the detected ArchiveType, or ``None`` when the format is
        unrecognised.  Used both for top-level artefacts (ZIP that is
        really Spark) and nested archives (``&DDC`` file that is really
        ZIP).

        Recognised signatures:
          ArcFS: ``Archive\\0`` or ``\\x1Aarchive``
          Spark: ``\\x1A`` followed by ``\\x00``, ``\\x80``–``\\x89``, or ``\\xFF``
          ZIP:   ``PK\\x03\\x04``
        """
        from shared.archive_formats import ArchiveType

        try:
            with open(file_path, 'rb') as fh:
                header = fh.read(8)
        except OSError:
            return None

        if len(header) < 2:
            return None

        # ArcFS: "Archive\0" or "\x1aarchive"
        if header[:8] == b'Archive\x00':
            return ArchiveType.ARCFS
        if len(header) >= 8 and header[0] == 0x1A and header[1:8] == b'archive':
            return ArchiveType.ARCFS

        # Spark: 0x1A followed by 0x00, 0x80-0x89, or 0xFF
        if header[0] == 0x1A:
            second = header[1]
            if second == 0x00 or (0x80 <= second <= 0x89) or second == 0xFF:
                return ArchiveType.SPARK

        # ZIP: PK\x03\x04
        if len(header) >= 4 and header[:4] == b'PK\x03\x04':
            return ArchiveType.ZIP

        return None

    # Extracted files with these extensions are promoted to derived artefacts
    # so they get their own analysis pipeline (e.g. an ISO inside a ZIP gets
    # FILE_EXTRACTION queued automatically).  Keep in sync with EXTENSION_MAP
    # in myapp/blueprints/artefacts.py.
    _PROMOTABLE_EXTENSIONS = {
        '.scp': ArtefactType.SCP,
        '.imd': ArtefactType.IMD,
        '.hfe': ArtefactType.HFE,
        '.adf': ArtefactType.RAW_SECTOR,
        '.img': ArtefactType.RAW_SECTOR,
        '.ima': ArtefactType.RAW_SECTOR,
        '.dsk': ArtefactType.RAW_SECTOR,
        '.dd':  ArtefactType.RAW_SECTOR,
        '.iso': ArtefactType.ISO,
    }

    def _extract_top_level_archive(
        self, analysis, artefact, work_dir,
        archive_type, archive_info,
        extract_zip, extract_tar, extract_rar, extract_7z,
    ):
        """Handle ARCHIVE_EXTRACT for a top-level artefact (no partition).

        Extracts the artefact file directly, creates a partition for the
        extracted files, queues follow-on analyses, and promotes any
        recognised disc images to derived artefacts.
        """
        import json
        from shared.archive_formats import ArchiveType, get_archive_info
        from .tools.extraction import enumerate_extracted_files
        from .config import OUTPUT_DIR
        from .utils.paths import get_output_path

        analysis_id = analysis['id']
        item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

        extract_dir = get_output_path(
            OUTPUT_DIR, item, artefact, analysis, partition=None
        )
        input_path = self.get_input_path(artefact, work_dir)

        # get_input_path() runs decompress_if_needed() which may have already
        # stripped the outer compression wrapper (e.g. .tar.gz → .tar), or
        # the file may be mis-labelled (named .tar.gz but actually plain tar).
        # In either case, downgrade to plain TAR so extract_tar() doesn't try
        # to decompress again.  This is safe because:
        #   - If decompression succeeded, the file is now a plain tar.
        #   - If decompression was skipped (not actually compressed), the file
        #     was already a plain tar despite the extension.
        _COMPRESSED_TAR_TYPES = {
            ArchiveType.TARGZ: ArchiveType.TAR,
            ArchiveType.TARBZ2: ArchiveType.TAR,
            ArchiveType.TARXZ: ArchiveType.TAR,
        }
        if archive_type in _COMPRESSED_TAR_TYPES:
            old_type = archive_type
            archive_type = _COMPRESSED_TAR_TYPES[archive_type]
            archive_info = get_archive_info(archive_type)
            log.info(f"Post-decompression: using {archive_type.value} (was {old_type.value})")

        # Sniff magic bytes — some RISC OS archives are distributed with
        # a .zip extension even though they are actually Spark or ArcFS.
        sniffed = self._sniff_archive_magic(input_path)
        if sniffed is not None and sniffed != archive_type:
            log.info(f"Magic-byte sniff overrides {archive_type.value} → {sniffed.value}")
            # A ZIP found via RISC OS filetype (including zip_riscos itself)
            # should be treated as ZIP_RISCOS so Acorn ,xxx suffixes are
            # parsed correctly and the container format is recorded accurately.
            _RISCOS_TYPES = (
                ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.PACKDIR,
                ArchiveType.TBAFS, ArchiveType.CFS, ArchiveType.SQUASH,
                ArchiveType.ZIP_RISCOS,
            )
            if sniffed == ArchiveType.ZIP and archive_type in _RISCOS_TYPES:
                sniffed = ArchiveType.ZIP_RISCOS
            archive_type = sniffed
            archive_info = get_archive_info(archive_type)

        # Dispatch to the correct extraction tool
        from .tools import extract_riscosarc

        if archive_type in [ArchiveType.SPARK, ArchiveType.ARCFS,
                            ArchiveType.PACKDIR, ArchiveType.CFS, ArchiveType.SQUASH]:
            result = extract_riscosarc(input_path, extract_dir)
            # Spark/ArcFS fallback: if riscosarc fails, the file might
            # actually be a ZIP with RISC OS filetypes (SparkFS uses
            # filetype &DDC for both Spark and ZIP).
            if not result['success'] and archive_type == ArchiveType.SPARK:
                result = extract_zip(input_path, extract_dir)
                if result['success']:
                    archive_type = ArchiveType.ZIP_RISCOS
                    archive_info = get_archive_info(archive_type)
        elif archive_type in [ArchiveType.ZIP, ArchiveType.ZIP_RISCOS]:
            result = extract_zip(input_path, extract_dir)
        elif archive_type in [ArchiveType.TAR, ArchiveType.TARGZ,
                              ArchiveType.TARBZ2, ArchiveType.TARXZ]:
            result = extract_tar(input_path, extract_dir, archive_type.value)
        elif archive_type == ArchiveType.RAR:
            result = extract_rar(input_path, extract_dir)
        elif archive_type == ArchiveType.SEVENZ:
            result = extract_7z(input_path, extract_dir)
        else:
            self.fail_analysis(
                analysis_id,
                f'Top-level extraction not supported for archive type: {archive_type.value}'
            )
            return

        if not result['success']:
            self.fail_analysis(
                analysis_id,
                result.get('error', 'Archive extraction failed'),
                tool_name=result.get('tool'),
                details=json.dumps({'process_output': result.get('process_output')}),
            )
            return

        files = enumerate_extracted_files(extract_dir, acorn='auto')

        partition = self.api.register_file_listing(
            artefact['uuid'], files, 'archive',
            container_format=archive_info['name'],
        )

        # Promote extracted files with recognised extensions to derived
        # artefacts so they get their own analysis pipeline.
        derived_count = 0
        for file_path in extract_dir.rglob('*'):
            if not file_path.is_file():
                continue
            ext = file_path.suffix.lower()
            artefact_type = self._PROMOTABLE_EXTENSIONS.get(ext)
            if artefact_type is None:
                continue
            resp = self.api.register_derived_artefact(
                analysis_id,
                label=file_path.name,
                source_path=file_path,
                artefact_type=artefact_type,
            )
            if resp:
                derived_count += 1
                log.info(f"Promoted {file_path.name} to derived {artefact_type.value} artefact")

        self.complete_analysis(
            analysis_id,
            tool_name=result['tool'],
            output_path=str(extract_dir),
            summary=f"Extracted {len(files)} files from {archive_info['name']}"
                    + (f" ({derived_count} promoted to artefacts)" if derived_count else ""),
            details=json.dumps({
                'file_count': len(files),
                'archive_type': archive_type.value,
                'derived_artefacts': derived_count,
            }),
        )

        if partition:
            self.queue_partition_follow_ups(
                artefact['uuid'],
                partition.get('uuid'),
                extraction_path=str(extract_dir),
            )

    @analysis_handler("archive extraction")
    def process_archive_extract(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process ARCHIVE_EXTRACT analysis.
        Extracts a specific archive file and registers the extracted files.

        When partition_uuid is present in hints, extracts an archive found
        inside a disc image (the original flow).  When partition_uuid is
        absent, the artefact itself is the archive (top-level upload) and
        the handler creates a new partition for the extracted files.
        """
        import json
        from shared.archive_formats import (
            ArchiveType,
            get_archive_info,
            is_compressor_format,
            is_disk_image_format,
        )
        from .tools import (
            extract_riscosarc,
            extract_tbafs,
            extract_zip,
            extract_tar,
            extract_rar,
            extract_7z,
            decompress_single_file,
            extract_acorn_disc_image_manager,
            extract_dos_7z,
        )
        from .tools.extraction import convert_fcfs_to_raw
        from .config import OUTPUT_DIR, MAX_ARCHIVE_DEPTH
        from .utils.paths import get_output_path

        analysis_id = analysis['id']

        hints = json.loads(analysis.get('hints') or '{}')

        file_id = hints.get('file_id')
        partition_uuid = hints.get('partition_uuid')
        archive_type_str = hints.get('archive_type')
        is_compressor = hints.get('is_compressor', False)
        extraction_depth = hints.get('extraction_depth', 1)
        hinted_extraction_path = hints.get('extraction_path')
        path_prefix = hints.get('path_prefix', '')

        # ── Top-level artefact archive ──────────────────────────────────
        # When no partition_uuid is provided, the artefact itself is the
        # archive (uploaded directly, not found inside a disc image).
        # Derive the archive type from the artefact type and delegate to
        # the shared extraction logic below after creating a partition.
        if not partition_uuid:
            artefact_type = artefact.get('artefact_type', '')
            if not archive_type_str:
                # Map ArtefactType value → ArchiveType value.  Most share
                # the same string values (zip, tar_gz, rar).  ARC needs
                # explicit mapping since ArtefactType.ARC ("arc") covers
                # both ArcFS and Spark; default to ArcFS and let the
                # magic-byte sniff in _extract_top_level_archive correct
                # it to Spark when appropriate.
                _ARTEFACT_TO_ARCHIVE = {
                    ArtefactType.ARC.value: ArchiveType.ARCFS.value,
                }
                archive_type_str = _ARTEFACT_TO_ARCHIVE.get(
                    artefact_type, artefact_type
                )

        # Get ArchiveType enum from string
        try:
            archive_type = ArchiveType(archive_type_str)
            archive_info = get_archive_info(archive_type)
        except (ValueError, KeyError):
            self.fail_analysis(analysis_id, f'Unknown archive type: {archive_type_str}')
            return

        # ── Top-level artefact archive (continued) ──────────────────────
        # Extract the artefact file directly and create a new partition
        # for the extracted files, then return.
        if not partition_uuid:
            self._extract_top_level_archive(
                analysis, artefact, work_dir,
                archive_type, archive_info,
                extract_zip, extract_tar, extract_rar, extract_7z,
            )
            return

        # Get partition and item metadata from API
        partition_resp = self.api.get(f"/partitions/{partition_uuid}")
        if not partition_resp:
            self.fail_analysis(analysis_id, 'Failed to get partition info')
            return

        partition = partition_resp.get('partition', {})

        # Find the file in the partition
        files_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000")
        if not files_resp:
            self.fail_analysis(analysis_id, 'Failed to get partition files')
            return

        # Find our specific file
        target_file = None
        for f in files_resp.get('files', []):
            if f['id'] == file_id:
                target_file = f
                break

        if not target_file:
            self.fail_analysis(analysis_id, f'File {file_id} not found in partition')
            return

        # Determine extraction path: prefer value passed through hints (set by the
        # analysis that triggered ARCHIVE_DETECT, which in turn queued this job).
        # Fall back to searching analyses only for jobs created before this fix.
        extraction_path = hinted_extraction_path
        if not extraction_path:
            artefact_uuid = artefact.get('uuid')
            analyses_resp = self.api.get(f"/artefacts/{artefact_uuid}/analysis")
            # Prefer file_extraction (always the disc-level extraction root);
            # only fall through to archive_extract entries if there is no
            # file_extraction with an output_path.
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
            extraction_path = file_extraction_path or archive_extract_path

        if not extraction_path:
            self.fail_analysis(analysis_id, 'Could not determine extraction path for files')
            return

        # Construct full path to archive file.
        # For nested archives, the DB path includes parent archive prefixes
        # (e.g. "OuterArchive/InnerArchive.zip") but on disk the file is
        # relative to the extraction directory without those prefixes.
        # Strip the path_prefix to get the on-disk relative path.
        db_path = target_file['path']
        if path_prefix and db_path.startswith(path_prefix + '/'):
            disk_relative_path = db_path[len(path_prefix) + 1:]
        else:
            disk_relative_path = db_path
        archive_path = Path(extraction_path) / disk_relative_path

        # Build a list of name variants to try in order.  DIM writes Acorn
        # files with a RISC OS filetype suffix (e.g. "Palette,DDC") in either
        # all-lowercase or all-uppercase.  Non-Acorn tools (7z, etc.) write the
        # plain name with no suffix.  Try the suffix variants first (when a
        # filetype is known), then the plain name as the final fallback.
        risc_os_filetype = target_file.get('risc_os_filetype')
        candidates = []
        if risc_os_filetype:
            candidates.append(Path(str(archive_path) + ',' + risc_os_filetype.lower()))
            candidates.append(Path(str(archive_path) + ',' + risc_os_filetype.upper()))
        candidates.append(archive_path)  # plain name: DOS, UNIX, or no-suffix fallback

        # For each candidate also try a Latin-1 byte variant.  Acorn filenames
        # can contain raw Latin-1 bytes (e.g. hard space 0xA0); sanitize_path()
        # converts these to proper Unicode (U+00A0) for the database, but the
        # file on disk still has the single raw byte.  Python would encode
        # U+00A0 as two UTF-8 bytes (0xC2 0xA0) when calling exists(), so we
        # also try a surrogate-escaped path that maps back to the raw byte.
        all_candidates = []
        for candidate in candidates:
            all_candidates.append(candidate)
            latin1_variant = make_latin1_fspath(str(candidate))
            if latin1_variant is not None:
                all_candidates.append(Path(latin1_variant))

        for candidate in all_candidates:
            if candidate.exists():
                archive_path = candidate
                break

        if not archive_path.exists():
            self.fail_analysis(analysis_id, f'Archive file not found at {archive_path}')
            return

        # Get item for hierarchical path
        item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

        # Create persistent output directory using hierarchical structure
        persistent_output = get_output_path(
            OUTPUT_DIR,
            item,
            artefact,
            analysis,
            partition
        )

        # Extract archive to temporary directory first
        temp_output_dir = work_dir / 'archive_contents'

        # Sniff magic bytes — filetype-based detection can be wrong (e.g.
        # &DDC is used for both Spark and ZIP on RISC OS).  Override the
        # archive_type when the file header tells us otherwise.
        sniffed = self._sniff_archive_magic(archive_path)
        if sniffed is not None and sniffed != archive_type:
            log.info(f"Magic-byte sniff overrides {archive_type.value} → {sniffed.value}")
            # A ZIP found via RISC OS filetype should be treated as
            # ZIP_RISCOS so Acorn ,xxx suffixes are parsed correctly.
            _RISCOS_TYPES = (
                ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.PACKDIR,
                ArchiveType.TBAFS, ArchiveType.CFS, ArchiveType.SQUASH,
                ArchiveType.ZIP_RISCOS,
            )
            if sniffed == ArchiveType.ZIP and archive_type in _RISCOS_TYPES:
                sniffed = ArchiveType.ZIP_RISCOS
            archive_type = sniffed
            archive_info = get_archive_info(archive_type)

        # Choose extraction method based on archive type
        if archive_type in [ArchiveType.ARCFS, ArchiveType.PACKDIR,
                            ArchiveType.SPARK, ArchiveType.CFS, ArchiveType.SQUASH]:
            result = extract_riscosarc(archive_path, temp_output_dir)

            # SparkFS filetypes Zip files as &DDC (Archive), which is also
            # used for Spark. If riscosarc unpacking fails, try Zip.
            # Upgrade archive_type to ZIP_RISCOS so the display name reflects
            # the actual container format while keeping is_acorn_archive True.
            if not result['success'] and archive_type == ArchiveType.SPARK:
                result = extract_zip(archive_path, temp_output_dir)
                if result['success']:
                    archive_type = ArchiveType.ZIP_RISCOS
                    archive_info = get_archive_info(archive_type)

        elif archive_type == ArchiveType.TBAFS:
            result = extract_tbafs(archive_path, temp_output_dir)

        elif archive_type == ArchiveType.FCFS:
            # Convert FCFS to raw, then extract as ADFS
            raw_path = work_dir / 'converted.img'
            conv_result = convert_fcfs_to_raw(archive_path, raw_path)
            if not conv_result['success']:
                result = conv_result
            else:
                # Extract the converted image
                result = extract_acorn_disc_image_manager(raw_path, temp_output_dir)

        elif archive_type == ArchiveType.DOSDISC:
            result = extract_dos_7z(archive_path, temp_output_dir)

        elif archive_type in [ArchiveType.ZIP, ArchiveType.ZIP_RISCOS]:
            result = extract_zip(archive_path, temp_output_dir)

        elif archive_type in [ArchiveType.TAR, ArchiveType.TARGZ,
                              ArchiveType.TARBZ2, ArchiveType.TARXZ]:
            result = extract_tar(archive_path, temp_output_dir, archive_type.value)

        elif archive_type == ArchiveType.RAR:
            result = extract_rar(archive_path, temp_output_dir)

        elif archive_type == ArchiveType.SEVENZ:
            result = extract_7z(archive_path, temp_output_dir)

        elif archive_type in [ArchiveType.GZIP, ArchiveType.BZIP2,
                              ArchiveType.XZ, ArchiveType.ZSTD]:
            # Single-file compressor - output with same name minus compression extension
            output_file = temp_output_dir / archive_path.stem
            result = decompress_single_file(archive_path, output_file, archive_type.value)

        else:
            self.fail_analysis(analysis_id, f'Unsupported archive type: {archive_type.value}')
            return

        if not result['success']:
            self.fail_analysis(
                analysis_id,
                result.get('error', 'Extraction failed'),
                tool_name=result.get('tool'),
                details=json.dumps({'process_output': result.get('process_output')})
            )
            return

        # Move extracted files from temp to persistent storage
        if temp_output_dir.exists():
            shutil.copytree(temp_output_dir, persistent_output, dirs_exist_ok=True)

        # Scan extracted files from persistent storage.
        # Paths are stored relative to the extraction output.  The API
        # will automatically prefix them with the archive's path when
        # registering (based on parent_file_id).
        archive_display_path = target_file['path']

        # RISC OS archive extractors preserve ,xxx filetype suffixes on
        # filenames.  Parse these to populate risc_os_filetype and strip
        # the suffix from display paths (same logic as FILE_EXTRACTION).
        is_acorn_archive = archive_type in (
            ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.ZIP_RISCOS,
            ArchiveType.PACKDIR, ArchiveType.TBAFS, ArchiveType.CFS,
            ArchiveType.SQUASH, ArchiveType.FCFS,
        )

        files = enumerate_extracted_files(
            persistent_output,
            acorn=is_acorn_archive,
            parent_file_id=file_id,
            extraction_depth=extraction_depth,
        )

        # Register extracted files in the same partition with parent_file_id
        if files:
            # Register files (they'll be added to the same partition)
            for i in range(0, len(files), 100):
                batch = files[i:i+100]
                file_records = []
                for f in batch:
                    record = {
                        'path': f['path'],
                        'filename': Path(f['path']).name,
                        'extension': Path(f['path']).suffix.lstrip('.').lower() or None,
                        'file_size': f.get('size'),
                        'parent_file_id': f.get('parent_file_id'),
                        'extraction_depth': f.get('extraction_depth'),
                        'md5': f.get('md5'),
                        'sha1': f.get('sha1'),
                        'sha256': f.get('sha256'),
                    }
                    if f.get('is_directory'):
                        record['is_directory'] = True
                    if f.get('risc_os_filetype'):
                        record['risc_os_filetype'] = f['risc_os_filetype']
                    file_records.append(record)
                self.api.post(f"/partitions/{partition_uuid}/files", {'files': file_records})

        # Queue ARCHIVE_DETECT for nested archives (if under depth limit).
        # Pass the archive's display path as path_prefix so that nested
        # ARCHIVE_EXTRACT jobs can strip it to locate files on disk.
        if extraction_depth < MAX_ARCHIVE_DEPTH:
            self.api.queue_analysis(
                artefact['uuid'],
                AnalysisType.ARCHIVE_DETECT.value,
                hints={
                    'partition_uuid': partition_uuid,
                    'extraction_path': str(persistent_output),
                    'path_prefix': archive_display_path,
                }
            )

        # Re-queue PRODUCT_RECOGNITION so the newly-extracted files are
        # included in folder matching.  The first PRODUCT_RECOGNITION run
        # (queued after the outer extraction) fires before this archive's
        # contents are registered, so it cannot see them.
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.PRODUCT_RECOGNITION.value,
            hints={'partition_uuid': partition_uuid},
        )

        # Queue FORMAT_CONVERT to scan for and convert any Sprite/Draw/Text files.
        # Pass path_prefix so that source_file values in analysis.details match
        # ExtractedFile.path in the database (which has the archive's display
        # path prepended for nested archives).
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.FORMAT_CONVERT.value,
            hints={
                'extraction_path': str(persistent_output),
                'path_prefix': archive_display_path,
            },
        )

        tool_key = result.get('tool', 'tool').lower().replace(' ', '_')
        po = result.get('process_output')
        details: dict = {
            'file_count': len(files),
            'extraction_depth': extraction_depth,
            'archive_type': archive_type.value,
        }
        if po:
            details[tool_key] = {'process_output': po}

        self.complete_analysis(
            analysis_id,
            tool_name=result['tool'],
            output_path=str(persistent_output),
            summary=f"Extracted {len(files)} files from {archive_info['name']} archive",
            details=json.dumps(details)
        )

    # RISC OS filetype suffixes that indicate viewable file types.
    # Mapping: suffix (e.g. ',ff9') → ArtefactType
    _RISCOS_VIEWABLE_SUFFIXES: dict[str, 'ArtefactType'] = {
        ',ff9': ArtefactType.ACORN_SPRITE,  # Sprite
        ',aff': ArtefactType.ACORN_DRAW,    # DrawFile
        ',fff': ArtefactType.ACORN_TEXT,    # Text
        ',feb': ArtefactType.ACORN_TEXT,    # Obey
        ',ffe': ArtefactType.ACORN_TEXT,    # Command
    }
    # Extension-based detection (used for DOS discs without RISC OS metadata)
    _EXT_VIEWABLE: dict[str, 'ArtefactType'] = {
        '.spr':  ArtefactType.ACORN_SPRITE,
        '.aff':  ArtefactType.ACORN_DRAW,
        '.draw': ArtefactType.ACORN_DRAW,
        '.txt':  ArtefactType.ACORN_TEXT,
    }

    def _convert_file_to_outputs(
        self,
        input_path: Path,
        artefact_type: 'ArtefactType',
        work_dir: Path,
        output_subdir: str | None,
        analysis_uuid: str,
        file_index: int = 0,
    ) -> list[dict] | None:
        """
        Convert a single viewable file and return a list of output dicts, or
        None if conversion failed (caller should call fail_analysis).

        ``file_index`` is used to make temporary subdirectory names unique when
        converting multiple files within one analysis run.
        """
        outputs = []

        if artefact_type == ArtefactType.ACORN_SPRITE:
            tmp_out = work_dir / f'sprites_{file_index}'
            result = convert_sprite(input_path, tmp_out, analysis_uuid)
            if not result['success']:
                log.warning(f"Sprite conversion failed for {input_path}: {result.get('error')}")
                return None
            for sprite in result['sprites']:
                # Include file_index in the saved name so that sprites from
                # different source files within the same analysis run don't
                # overwrite each other.  sprite['path'].name is already
                # f'{analysis_uuid}_{idx:02d}_{safe_name}.png'; insert
                # file_index after the uuid prefix.
                orig_stem = sprite['path'].stem  # '{uuid}_{idx}_{name}'
                rest = orig_stem[len(analysis_uuid) + 1:]  # '{idx}_{name}'
                unique_name = f'{analysis_uuid}_{file_index}_{rest}.png'
                saved = self.save_output_file(
                    sprite['path'],
                    unique_name,
                    subdir=output_subdir,
                )
                outputs.append({
                    'type': 'image',
                    'filename': saved,
                    'name': sprite['name'],
                    'description': sprite['name'],
                    'tool': 'spritefile',
                })

        elif artefact_type == ArtefactType.ACORN_DRAW:
            from .tools.extraction import parse_acorn_filename as _parse_acorn
            true_name, _ = _parse_acorn(input_path.name)
            tmp_out = work_dir / f'draw_{file_index}'
            result = convert_draw(input_path, tmp_out, analysis_uuid)
            if not result['success']:
                return None  # convert_draw already logged the full error
            # Include file_index so multiple Draw files in the same archive
            # each get a unique output filename rather than overwriting each other.
            saved_png = self.save_output_file(
                result['png_path'],
                f'{analysis_uuid}_{file_index}_draw.png',
                subdir=output_subdir,
            )
            entry = {
                'type': 'image',
                'filename': saved_png,
                'name': true_name,
                'description': true_name,
                'tool': 'drawfile_render',
            }
            if result['svg_path']:
                # Save SVG and link it on the PNG entry so the viewer can use
                # PNG as a thumbnail and open SVG on click.
                saved_svg = self.save_output_file(
                    result['svg_path'],
                    f'{analysis_uuid}_{file_index}_draw.svg',
                    subdir=output_subdir,
                )
                entry['svg_filename'] = saved_svg
            outputs.append(entry)

        elif artefact_type == ArtefactType.ACORN_TEXT:
            from .tools.extraction import parse_acorn_filename as _parse_acorn
            true_name, _ = _parse_acorn(input_path.name)
            try:
                raw = input_path.read_bytes()
                # Decode as Latin-1 (covers all Acorn/DOS byte values);
                # normalise RISC OS line endings (0x0A) to LF.
                text = raw.decode('latin-1').replace('\r\n', '\n').replace('\r', '\n')
                out_filename = f'{analysis_uuid}_{file_index}_text.txt'
                out_path = work_dir / out_filename
                out_path.write_text(text, encoding='utf-8')
                saved = self.save_output_file(out_path, out_filename, subdir=output_subdir)
                outputs.append({
                    'type': 'text',
                    'filename': saved,
                    'name': true_name,
                    'description': true_name,
                    'tool': 'builtin',
                })
            except Exception as e:
                log.warning(f"Text conversion failed for {input_path}: {e}")
                return None

        return outputs

    def _detect_viewable_type(self, path: Path) -> 'ArtefactType | None':
        """Return the ArtefactType for a viewable file, or None if not viewable."""
        name_lower = path.name.lower()
        for suffix, atype in self._RISCOS_VIEWABLE_SUFFIXES.items():
            if name_lower.endswith(suffix):
                return atype
        return self._EXT_VIEWABLE.get(path.suffix.lower())

    @analysis_handler("format conversion")
    def process_format_convert(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FORMAT_CONVERT analysis.  Supports two modes:

        Mode 1 — Direct artefact (artefact_type is ACORN_SPRITE/DRAW/TEXT):
          Convert the artefact's own file.  Used for directly-uploaded Acorn
          files; triggered via ANALYSIS_MAP.

        Mode 2 — Extraction scan (hints contain 'extraction_path'):
          Scan the extraction output directory for every viewable file, convert
          each one, and store outputs with a 'source_file' field matching
          ExtractedFile.path (display path, Acorn filetype suffix stripped).
          Queued automatically by queue_partition_follow_ups() after every
          FILE_EXTRACTION and ARCHIVE_EXTRACT.
        """
        from .tools.extraction import _has_acorn_filetypes

        analysis_id = analysis['id']
        analysis_uuid = analysis['uuid']
        artefact_type_str = artefact.get('artefact_type', '')
        hints = json.loads(analysis.get('hints') or '{}')

        item = artefact.get('item', {})
        item_uuid = item.get('uuid', '')
        item_slug = item.get('slug', '')
        artefact_uuid = artefact.get('uuid', '')
        artefact_slug = artefact.get('slug', '')
        item_part = f"{item_uuid}_{item_slug}" if item_slug else item_uuid
        artefact_part = f"{artefact_uuid}_{artefact_slug}" if artefact_slug else artefact_uuid
        output_subdir = f"{item_part}/{artefact_part}" if (item_part and artefact_part) else None

        _direct_types = (
            ArtefactType.ACORN_SPRITE.value,
            ArtefactType.ACORN_DRAW.value,
            ArtefactType.ACORN_TEXT.value,
        )

        # --- Mode 1: Direct artefact conversion ---
        if artefact_type_str in _direct_types:
            input_path = self.get_input_path(artefact, work_dir)
            artefact_type = ArtefactType(artefact_type_str)
            outputs = self._convert_file_to_outputs(
                input_path, artefact_type, work_dir, output_subdir, analysis_uuid,
            )
            if outputs is None:
                self.fail_analysis(analysis_id, f'Conversion failed for {artefact_type_str}')
                return
            self.complete_analysis(
                analysis_id,
                summary=f'Converted {len(outputs)} output(s) for {artefact_type_str}',
                details=json.dumps({
                    'artefact_type': artefact_type_str,
                    'outputs': outputs,
                }),
            )
            return

        # --- Mode 2: Extraction scan ---
        extraction_path = hints.get('extraction_path')
        if not extraction_path:
            self.fail_analysis(
                analysis_id,
                f'FORMAT_CONVERT not supported for artefact type {artefact_type_str!r} '
                f'and no extraction_path hint provided',
            )
            return

        extract_dir = Path(extraction_path)
        if not extract_dir.is_dir():
            self.fail_analysis(
                analysis_id,
                f'Extraction directory not found: {extraction_path}',
            )
            return

        # Auto-detect Acorn filetype suffixes for display-path computation.
        # Display paths must match ExtractedFile.path (Acorn suffix stripped).
        is_acorn = _has_acorn_filetypes(extract_dir)

        # path_prefix is set when FORMAT_CONVERT was queued from process_archive_extract
        # for a nested archive (e.g. an archive file on a disc image).  The DB
        # registers those files with the archive's display path prepended, so
        # source_file must include the same prefix to match ExtractedFile.path.
        path_prefix = hints.get('path_prefix', '')  # e.g. 'Archives/Emulators.zip'

        all_outputs = []
        file_index = 0
        for path in sorted(extract_dir.rglob('*')):
            if not path.is_file():
                continue
            viewable_type = self._detect_viewable_type(path)
            if viewable_type is None:
                continue

            # Compute display path (matching ExtractedFile.path in the database)
            rel = path.relative_to(extract_dir)
            if is_acorn:
                true_name, _ = parse_acorn_filename(path.name)
                if len(rel.parts) > 1:
                    display_path = str(Path(*rel.parts[:-1]) / true_name)
                else:
                    display_path = true_name
            else:
                display_path = str(rel)

            # Prepend archive path prefix for nested archives so the path matches
            # ExtractedFile.path (which has the parent archive path prepended).
            if path_prefix:
                display_path = path_prefix + '/' + display_path

            file_outputs = self._convert_file_to_outputs(
                path, viewable_type, work_dir, output_subdir, analysis_uuid, file_index,
            )
            file_index += 1
            if file_outputs is None:
                log.warning(f"Skipping {path} — conversion failed")
                continue
            for out in file_outputs:
                out['source_file'] = display_path
            all_outputs.extend(file_outputs)

        self.complete_analysis(
            analysis_id,
            summary=f'Converted {len(all_outputs)} output(s) from {file_index} viewable file(s)',
            details=json.dumps({
                'mode': 'extraction_scan',
                'outputs': all_outputs,
            }),
        )

    @analysis_handler("product recognition")
    def process_product_recognition(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process PRODUCT_RECOGNITION analysis.

        Fetches all hash databases that have product recognition enabled, then
        checks the extracted files in each partition of this artefact against
        the product definitions.  A product is matched when all of its required
        files (identified by MD5/SHA1 hash) are present in a single directory.
        Optional files increase the confidence score but are not required.
        When path_match_enabled is set on the product, the file's relative path
        within the matched folder is also checked against the stored relative_path.

        Results are reported back via POST /partitions/<uuid>/recognised-products.
        """
        import json as _json

        analysis_id = analysis['id']
        hints = _json.loads(analysis.get('hints') or '{}')
        partition_uuid = hints.get('partition_uuid')

        if not partition_uuid:
            self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
            return

        # Fetch recognition config (all enabled databases with products)
        config = self.api.get_recognition_config()
        if not config:
            # Nothing to do — no recognition-enabled databases
            self.complete_analysis(analysis_id, summary='No recognition-enabled hash databases configured')
            return

        # Fetch all files in this partition (may be large; page through them)
        page = 1
        all_files = []
        while True:
            resp = self.api.get(
                f'/partitions/{partition_uuid}/files?per_page=10000&page={page}&show_known=true'
            )
            if not resp:
                break
            batch = resp.get('files', [])
            all_files.extend(batch)
            if page >= resp.get('pages', 1):
                break
            page += 1

        if not all_files:
            self.complete_analysis(analysis_id, summary='No extracted files in partition')
            return

        # Build index: folder_path -> {hash_set, relative_path_map}
        # folder_path is the parent directory of each file (i.e. path up to last '/')
        # hash_set: set of (md5, sha1) tuples (lowercased)
        # path_map: relative_path_within_folder -> (md5, sha1)
        folder_index: dict[str, dict] = {}
        for f in all_files:
            if f.get('is_directory'):
                continue
            path = f.get('path', '')
            if '/' in path:
                folder = path.rsplit('/', 1)[0]
                rel = path.rsplit('/', 1)[1]
            else:
                folder = ''
                rel = path

            if folder not in folder_index:
                folder_index[folder] = {'hashes': set(), 'path_map': {}}

            md5 = (f.get('md5') or '').lower()
            sha1 = (f.get('sha1') or '').lower()
            if md5 or sha1:
                folder_index[folder]['hashes'].add((md5, sha1))
                folder_index[folder]['path_map'][rel.lower()] = (md5, sha1)

        # Check each product across all databases against each folder
        results = []
        total_products = sum(len(db.get('products', [])) for db in config)

        for db in config:
            for product in db.get('products', []):
                product_id = product['product_id']
                path_match_enabled = product.get('path_match_enabled', False)
                required_files = product.get('required_files', [])
                optional_files = product.get('optional_files', [])

                if not required_files and not optional_files:
                    continue

                for folder, idx in folder_index.items():
                    folder_hashes = idx['hashes']
                    path_map = idx['path_map']

                    # Check required files (all must match)
                    # When path matching is enabled, relative_path in the product
                    # config is the full root-relative path (e.g. '!ArcFS/ArcFS'),
                    # but path_map keys are only the filename within the folder
                    # (e.g. 'arcfs').  Pre-compute the folder prefix to strip.
                    folder_lower = folder.lower()
                    folder_prefix = folder_lower + '/' if folder_lower else ''

                    required_matched = 0
                    for req in required_files:
                        md5 = (req.get('md5') or '').lower()
                        sha1 = (req.get('sha1') or '').lower()
                        rel_path = (req.get('relative_path') or '').lower()

                        matched = False
                        if path_match_enabled and rel_path:
                            # Must match both hash AND relative path.
                            # Strip the folder prefix so '!arcfs/arcfs' becomes
                            # 'arcfs' before looking up in path_map.
                            if folder_prefix and rel_path.startswith(folder_prefix):
                                rel_path_in_folder = rel_path[len(folder_prefix):]
                            else:
                                rel_path_in_folder = rel_path
                            if rel_path_in_folder in path_map:
                                file_md5, file_sha1 = path_map[rel_path_in_folder]
                                matched = (
                                    (md5 and file_md5 == md5) or
                                    (sha1 and file_sha1 == sha1)
                                )
                        else:
                            # Hash-only match: any file in the folder with this hash
                            matched = any(
                                (md5 and h[0] == md5) or (sha1 and h[1] == sha1)
                                for h in folder_hashes
                            )

                        if matched:
                            required_matched += 1

                    if required_files and required_matched < len(required_files):
                        continue  # Not a match — not all required files found

                    # Count optional matches
                    optional_matched = 0
                    for opt in optional_files:
                        md5 = (opt.get('md5') or '').lower()
                        sha1 = (opt.get('sha1') or '').lower()
                        rel_path = (opt.get('relative_path') or '').lower()

                        if path_match_enabled and rel_path:
                            if folder_prefix and rel_path.startswith(folder_prefix):
                                rel_path_in_folder = rel_path[len(folder_prefix):]
                            else:
                                rel_path_in_folder = rel_path
                            if rel_path_in_folder in path_map:
                                file_md5, file_sha1 = path_map[rel_path_in_folder]
                                if (md5 and file_md5 == md5) or (sha1 and file_sha1 == sha1):
                                    optional_matched += 1
                        else:
                            if any(
                                (md5 and h[0] == md5) or (sha1 and h[1] == sha1)
                                for h in folder_hashes
                            ):
                                optional_matched += 1

                    # For optional-only products, require at least one match
                    if not required_files and optional_matched == 0:
                        continue

                    results.append({
                        'product_id': product_id,
                        'folder_path': folder if folder else '/',
                        'required_matched': required_matched,
                        'required_total': len(required_files),
                        'optional_matched': optional_matched,
                        'optional_total': len(optional_files),
                    })

        self.api.report_recognised_products(partition_uuid, results)

        self.complete_analysis(
            analysis_id,
            summary=f'Checked {total_products} product(s) against {len(folder_index)} folder(s); {len(results)} match(es) found'
        )

    @analysis_handler("disc mastering data detection")
    def process_disc_mastering_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process DISC_MASTERING_DETECT analysis.

        Scans the trailing tracks of an HFE image for mastering/duplicator
        fingerprint data (TRACEBACK format and Formaster record).
        """
        from .tools.hfe import analyse_hfe_mastering

        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        result = analyse_hfe_mastering(input_path, scan_count=MASTERING_TRACK_SCAN_COUNT)
        indicators = result.get('indicators', [])
        if indicators:
            types_found = ', '.join(sorted({i['type'] for i in indicators}))
            summary = f"Mastering data found: {types_found}"
        else:
            summary = "No mastering data found"
        self.complete_analysis(
            analysis_id,
            tool_name='hfe_parser',
            summary=summary,
            details=json.dumps(result),
        )

    @analysis_handler("disc copy protection detection")
    def process_disc_protection_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process DISC_PROTECTION_DETECT analysis.

        Scans all tracks of an HFE image for copy protection indicators:
        weak/fuzzy bits, intentional bad CRCs, cylinder ID mismatches,
        deleted data address marks, and duplicate sector IDs.
        """
        from .tools.hfe import analyse_hfe_protection

        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        result = analyse_hfe_protection(input_path)
        indicators = result.get('indicators', [])
        if indicators:
            types_found = ', '.join(sorted({i['type'] for i in indicators}))
            summary = f"Protection indicators found: {types_found}"
        else:
            summary = "No protection indicators found"
        self.complete_analysis(
            analysis_id,
            tool_name='hfe_parser',
            summary=summary,
            details=json.dumps(result),
        )

    @analysis_handler("ARMlock removal")
    def process_armlock_remove(self, analysis: dict, artefact: dict, work_dir: Path):
        """Remove ARMlock disc security from a confirmed-protected ADFS disc image.

        This handler is only queued by PARTITION_DETECT when the ARMlock signature
        has already been found on an ADFS partition.  It re-runs detection to capture
        full details (zone map state, directory listings, ARMlock module bytes), then
        removes the protection and hands off to FILE_EXTRACTION.

        ARMlock (by Digital Services) protects ADFS discs by replacing the real root
        directory with a stripped "demo" copy and stashing the original at disc address
        0x400.  It also encodes the boot_option field in both copies of the zone map.
        Disc Image Manager cannot extract files from a protected image because the root
        directory it sees is fake.
        """
        analysis_id = analysis['id']
        hints = json.loads(analysis.get('hints') or '{}')
        filesystem_hint = hints.get('filesystem', 'adfs')
        partition_index = hints.get('partition_index', 0)

        # Use cached decompressed path if available (from PARTITION_DETECT).
        partition_image_path = hints.get('partition_image_path')
        if partition_image_path:
            input_path = Path(partition_image_path)
        else:
            input_path = self.get_input_path(artefact, work_dir)

        # Re-run detection to capture full details for the analysis record.
        detection = detect_armlock(input_path)

        if not detection.get('detected'):
            # Shouldn't happen (PARTITION_DETECT confirmed protection), but handle
            # it defensively rather than leaving FILE_EXTRACTION unqueued.
            log.warning(
                f"ARMLOCK_REMOVE for analysis {analysis_id}: protection not found on "
                f"second pass — queuing FILE_EXTRACTION directly"
            )
            self.queue_file_extraction(
                artefact['uuid'],
                filesystem_hint,
                partition_index,
                partition_image_path=partition_image_path,
            )
            self.complete_analysis(
                analysis_id,
                tool_name='armlock',
                summary='ARMlock signature not found on second pass; FILE_EXTRACTION queued directly',
                details=json.dumps(detection),
            )
            return

        # Remove protection and register a cleaned artefact.
        cleaned_path = work_dir / 'armlock_removed.img'
        removal = remove_armlock(input_path, cleaned_path)

        # Serialise module bytes as hex for JSON storage; also register as a
        # derived UNKNOWN artefact so it can be downloaded for offline analysis.
        # The module contains the protection code and password data.
        # Exclude raw module bytes (stored separately as an artefact) and the
        # real_root listing (FILE_EXTRACTION on the cleaned image covers this).
        details: dict = {k: v for k, v in detection.items()
                         if k not in ('module_data', 'real_root', 'stripped_root')}
        if detection.get('module_data'):
            details['module_data_length'] = len(detection['module_data'])
            module_path = work_dir / 'ARMlock_module'
            module_path.write_bytes(detection['module_data'])
            module_label = f'{artefact.get("label", "Unknown")} (ARMlock module)'
            if detection.get('module_version'):
                module_label = f'{artefact.get("label", "Unknown")} (ARMlock module {detection["module_version"]})'
            module_artefact = self.api.register_derived_artefact(
                analysis_id,
                module_label,
                module_path,
                ArtefactType.UNKNOWN,
                auto_analyse=False,
            )
            if module_artefact:
                details['module_artefact_uuid'] = module_artefact['artefact']['uuid']
        details['removal'] = removal

        if removal['success']:
            cleaned = self.api.register_derived_artefact(
                analysis_id,
                f'{artefact.get("label", "Unknown")} (ARMlock removed)',
                cleaned_path,
                ArtefactType.RAW_SECTOR,
                auto_analyse=False,
            )
            if cleaned:
                cleaned_uuid = cleaned['artefact']['uuid']
                self.queue_file_extraction(cleaned_uuid, filesystem_hint, partition_index)
                summary = (
                    f'ARMlock disc security detected and removed. '
                    f'Cleaned artefact queued for file extraction.'
                )
                if detection.get('module_data'):
                    summary += ' ARMlock module saved.'
            else:
                summary = 'ARMlock detected; failed to register cleaned artefact.'
        else:
            summary = f'ARMlock detected but removal failed: {removal.get("error")}'

        self.complete_analysis(
            analysis_id,
            tool_name='armlock',
            summary=summary,
            details=json.dumps(details),
        )

    @analysis_handler("RISC OS module parse")
    def process_riscos_module_parse(self, analysis: dict, artefact: dict, work_dir: Path):
        """Parse RISC OS relocatable modules found in an extraction.

        Scans partition files for filetype ffa (Module), reads each from disk,
        and extracts metadata (title, version, date, SWIs, star commands).
        Only queued for Acorn filesystem extractions.
        """
        from .tools.riscos_module import decode_module, ModuleParseError

        analysis_id = analysis['id']
        hints = json.loads(analysis.get('hints') or '{}')
        partition_uuid = hints.get('partition_uuid')
        extraction_path = hints.get('extraction_path')

        if not partition_uuid:
            self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
            return

        # Fetch files with RISC OS filetype ffa (Module)
        files_resp = self.api.get(
            f"/partitions/{partition_uuid}/files?per_page=10000&show_known=true"
        )
        if not files_resp:
            self.fail_analysis(analysis_id, 'Failed to get partition files')
            return

        module_files = [
            f for f in files_resp.get('files', [])
            if (f.get('risc_os_filetype') or '').lower() == 'ffa'
            and not f.get('is_directory', False)
        ]

        if not module_files:
            self.complete_analysis(
                analysis_id,
                summary='No RISC OS modules (filetype ffa) found',
                details=json.dumps({'modules': [], 'files_scanned': 0}),
            )
            return

        # Determine extraction path (same logic as ARCHIVE_EXTRACT)
        if not extraction_path:
            artefact_uuid = artefact.get('uuid')
            analyses_resp = self.api.get(f"/artefacts/{artefact_uuid}/analysis")
            for a in analyses_resp.get('analyses', []):
                if a.get('analysis_type') == 'file_extraction' and a.get('output_path'):
                    extraction_path = a['output_path']
                    break

        if not extraction_path:
            self.fail_analysis(analysis_id, 'Could not determine extraction path')
            return

        extract_dir = Path(extraction_path)
        modules = []
        parse_errors = 0

        for file_data in module_files:
            db_path = file_data['path']
            risc_os_filetype = file_data.get('risc_os_filetype', '')

            # Build candidate paths (with Acorn filetype suffix variants)
            candidates = []
            if risc_os_filetype:
                candidates.append(extract_dir / (db_path + ',' + risc_os_filetype.lower()))
                candidates.append(extract_dir / (db_path + ',' + risc_os_filetype.upper()))
            candidates.append(extract_dir / db_path)

            # Also try Latin-1 byte variants
            all_candidates = []
            for candidate in candidates:
                all_candidates.append(candidate)
                latin1_variant = make_latin1_fspath(str(candidate))
                if latin1_variant is not None:
                    all_candidates.append(Path(latin1_variant))

            file_path = None
            for candidate in all_candidates:
                if candidate.is_file():
                    file_path = candidate
                    break

            if file_path is None:
                log.warning(f"Module file not found on disk: {db_path}")
                parse_errors += 1
                continue

            try:
                data = file_path.read_bytes()
                result = decode_module(data)
                result['file_path'] = db_path
                # Exclude bulky fields from the stored details
                result.pop('commands', None)
                result.pop('help_string', None)
                modules.append(result)
            except ModuleParseError as e:
                log.warning(f"Could not parse module {db_path}: {e}")
                parse_errors += 1
            except Exception as e:
                log.warning(f"Unexpected error parsing module {db_path}: {e}")
                parse_errors += 1

        summary_parts = [f'Parsed {len(modules)} RISC OS module(s)']
        if parse_errors:
            summary_parts.append(f'{parse_errors} could not be parsed')

        self.complete_analysis(
            analysis_id,
            tool_name='riscos_module_parser',
            summary=', '.join(summary_parts),
            details=json.dumps({
                'modules': modules,
                'files_scanned': len(module_files),
                'parse_errors': parse_errors,
            }),
        )

    # =========================================================================
    # Job Processing
    # =========================================================================

    def process_analysis(self, analysis: dict):
        """Process a single analysis job."""
        analysis_id = analysis['id']
        analysis_type = analysis['analysis_type']
        artefact = analysis.get('artefact', {})

        log.info(f"Processing analysis {analysis_id}: {analysis_type} for {artefact.get('label', 'unknown')}")

        # Status is already RUNNING from the atomic claim in claim_and_process().

        # Create temporary work directory
        with tempfile.TemporaryDirectory(prefix=f'arcology_{analysis_id}_') as work_dir:
            work_path = Path(work_dir)

            try:
                # Dispatch to appropriate handler
                handlers = {
                    AnalysisType.CHECKSUM_COMPUTE.value: self.process_checksum_compute,
                    AnalysisType.FLUX_VISUALISATION.value: self.process_flux_visualisation,
                    AnalysisType.FLUX_DECODE.value: self.process_flux_decode,
                    AnalysisType.FILE_EXTRACTION.value: self.process_file_extraction,
                    AnalysisType.METADATA_EXTRACT.value: self.process_metadata_extract,
                    AnalysisType.FORMAT_IDENTIFY.value: self.process_format_identify,
                    AnalysisType.PARTITION_DETECT.value: self.process_partition_detect,
                    AnalysisType.ARCHIVE_DETECT.value: self.process_archive_detect,
                    AnalysisType.ARCHIVE_EXTRACT.value: self.process_archive_extract,
                    AnalysisType.PRODUCT_RECOGNITION.value: self.process_product_recognition,
                    AnalysisType.DISC_MASTERING_DETECT.value: self.process_disc_mastering_detect,
                    AnalysisType.DISC_PROTECTION_DETECT.value: self.process_disc_protection_detect,
                    AnalysisType.ARMLOCK_REMOVE.value: self.process_armlock_remove,
                    AnalysisType.FORMAT_CONVERT.value: self.process_format_convert,
                    AnalysisType.RISCOS_MODULE_PARSE.value: self.process_riscos_module_parse,
                }

                handler = handlers.get(analysis_type)
                if handler:
                    handler(analysis, artefact, work_path)
                else:
                    log.warning(f"Unknown analysis type: {analysis_type}")
                    self.fail_analysis(analysis_id, f'Unknown analysis type: {analysis_type}')

            except Exception as e:
                log.exception(f"Analysis {analysis_id} failed with exception")
                try:
                    self.fail_analysis(analysis_id, str(e)[:1000])
                except Exception:
                    log.exception(
                        f"Analysis {analysis_id}: failed to report failure to API "
                        f"— job may remain in 'running' state"
                    )

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

            if self.api.claim_analysis(analysis_id):
                # Successfully claimed - process it
                log.info(f"Claimed analysis {analysis_id}")
                self.process_analysis(analysis)
                return 1
            else:
                # Already claimed by another worker, try next
                log.debug(f"Analysis {analysis_id} already claimed, trying next")
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
