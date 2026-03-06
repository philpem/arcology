"""
Analysis worker and job processing.

Contains the main AnalysisWorker class that polls for jobs and
dispatches them to appropriate handlers.
"""

import functools
import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from .config import log, POLL_INTERVAL, MASTERING_TRACK_SCAN_COUNT
from .types import ArtefactType, AnalysisType
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
            try:
                return fn(self, analysis, artefact, work_dir)
            except Exception as e:
                log.exception(f"Analysis {analysis_id} failed during {description}")
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
                        f"Analysis {analysis_id}: failed to report failure to API "
                        f"— job may remain in 'running' state"
                    )
        return wrapper
    return decorator


class AnalysisWorker:
    """Main worker class that processes analysis jobs."""

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path):
        """
        Initialize the worker.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded artefacts are stored
            output_dir: Directory for analysis outputs (e.g., visualisations)
        """
        self.uploads = upload_dir
        self.outputs = output_dir
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.api = ArcologyAPI(api_url, upload_dir, output_dir)
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

    def save_output_file(self, source_path: Path, filename: str) -> str:
        """
        Save an output file (like a visualisation) to the outputs directory.

        Args:
            source_path: Path to the generated file
            filename: Destination filename

        Returns:
            The filename (relative path for URLs)
        """
        dest_path = self.outputs / filename
        shutil.copy(source_path, dest_path)
        return filename

    # =========================================================================
    # Analysis Handlers
    # =========================================================================

    @analysis_handler("flux visualisation")
    def process_flux_visualisation(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process FLUX_VISUALISATION analysis."""
        analysis_id = analysis['id']
        analysis_uuid = analysis['uuid']

        input_path = self.get_input_path(artefact, work_dir)

        outputs = []

        # Try Fluxfox first (more detailed)
        # Use analysis UUID to prevent overwrites when re-running analysis
        output_fluxfox = work_dir / f"{analysis_uuid}_fluxfox.png"
        result_fluxfox = flux_visualisation_fluxfox(input_path, output_fluxfox)

        if result_fluxfox['success']:
            saved_name = self.save_output_file(output_fluxfox, f"{analysis_uuid}_fluxfox.png")
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
            saved_name = self.save_output_file(output_hxcfe, f"{analysis_uuid}_hxcfe.png")
            outputs.append({
                'tool': 'hxcfe',
                'type': 'image',
                'filename': saved_name,
                'description': 'HxCFE visualisation'
            })

        if outputs:
            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name='fluxfox,hxcfe',
                summary=f'Generated {len(outputs)} flux visualisation(s)',
                details=json.dumps({
                    'outputs': outputs,
                    'fluxfox': result_fluxfox,
                    'hxcfe': result_hxcfe
                })
            )
        else:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f"Fluxfox: {result_fluxfox.get('error', 'unknown')}; HxCFE: {result_hxcfe.get('error', 'unknown')}"
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

        self.api.update_analysis(
            analysis_id,
            status='completed' if any_success else 'failed',
            success=any_success,
            tool_name='hxcfe,greaseweazle',
            summary='; '.join(summary_parts),
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
            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=False,
                error_message=f'File extraction not supported for {artefact_type} format. Only raw sector images are supported.',
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
            # No filesystem hint - try Acorn DIM first (it will fail quickly
            # and cleanly on non-Acorn images), then fall back to 7z.
            # We must NOT try 7z first because it will "succeed" on ADFS
            # images containing ZIP files by extracting the embedded ZIP
            # instead of the actual disc filesystem.
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
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                tool_name=result.get('tool'),
                error_message=result.get('error', 'Extraction failed'),
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

        # Register partition and file listing in the database
        partition = self.api.register_file_listing(
            artefact['uuid'],
            files,
            fs_type,
            label=disc_name,
            container_format=container_format,
            partition_index=partition_index,
        )

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            tool_name=result['tool'],
            summary=f'Extracted {len(files)} files ({fs_type})',
            output_path=str(extract_dir),
            details=_build_details({'file_count': len(files)})
        )

        # Queue ARCHIVE_DETECT to scan extracted files for nested archives
        if partition:
            self.api.queue_analysis(
                artefact['uuid'],
                AnalysisType.ARCHIVE_DETECT.value,
                hints={
                    'partition_uuid': partition.get('uuid'),
                    'extraction_path': str(extract_dir),
                }
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

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
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
                self.api.update_analysis(
                    analysis_id,
                    status='failed',
                    success=False,
                    tool_name='fcfs2raw',
                    error_message=f'FCFS detected but conversion failed: {conv_result.get("error", "unknown")}',
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

            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=True,
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
        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
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

        # 5. If nothing detected, report whole disc as single unknown partition
        if not detected_partitions:
            file_size = input_path.stat().st_size
            detected_partitions = [{
                'index': 0,
                'start_byte': 0,
                'filesystem': filesystem_hint or 'unknown',
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
                    self.api.queue_analysis(
                        derived_uuid,
                        AnalysisType.FILE_EXTRACTION.value,
                        hints={'filesystem': fs, 'partition_index': idx}
                    )
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

            # Queue FILE_EXTRACTION against the original artefact
            for partition in detected_partitions:
                idx = partition['index']
                fs = partition.get('filesystem', 'unknown')
                extraction_hints: dict = {'filesystem': fs, 'partition_index': idx}
                if idx in partition_image_paths:
                    extraction_hints['partition_image_path'] = partition_image_paths[idx]
                self.api.queue_analysis(
                    artefact['uuid'],
                    AnalysisType.FILE_EXTRACTION.value,
                    hints=extraction_hints,
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

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
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
        from .archive_formats import (
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
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='No partition_uuid in analysis hints'
            )
            return

        # Get files not yet marked as archives (skip already-detected ones).
        # Must include known files (show_known=true) because archive files
        # can match the known-files database and would otherwise be hidden.
        partition_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000&is_archive=false&show_known=true")
        if not partition_resp:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Failed to get partition files'
            )
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

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            summary=summary,
            details=json.dumps({
                'archives_found': archive_count,
                'compressors_found': compressor_count,
                'depth_limit_exceeded': depth_limit_exceeded
            })
        )

    @analysis_handler("archive extraction")
    def process_archive_extract(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process ARCHIVE_EXTRACT analysis.
        Extracts a specific archive file and registers the extracted files.

        Performs actual extraction with file path resolution.
        """
        import json
        from .archive_formats import (
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

        # Get ArchiveType enum from string
        try:
            archive_type = ArchiveType(archive_type_str)
            archive_info = get_archive_info(archive_type)
        except (ValueError, KeyError):
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f'Unknown archive type: {archive_type_str}'
            )
            return

        # Get partition and item metadata from API
        partition_resp = self.api.get(f"/partitions/{partition_uuid}")
        if not partition_resp:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Failed to get partition info'
            )
            return

        partition = partition_resp.get('partition', {})

        # Find the file in the partition
        files_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000")
        if not files_resp:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Failed to get partition files'
            )
            return

        # Find our specific file
        target_file = None
        for f in files_resp.get('files', []):
            if f['id'] == file_id:
                target_file = f
                break

        if not target_file:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f'File {file_id} not found in partition'
            )
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
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Could not determine extraction path for files'
            )
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
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f'Archive file not found at {archive_path}'
            )
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
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f'Unsupported archive type: {archive_type.value}'
            )
            return

        if not result['success']:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=result.get('error', 'Extraction failed'),
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

        files = []
        for file_path in persistent_output.rglob('*'):
            if not file_path.is_file():
                continue

            # Skip .inf metadata files (Acorn extraction artifacts)
            if is_acorn_archive and file_path.suffix == '.inf':
                continue

            rel_path = file_path.relative_to(persistent_output)

            file_entry = {
                'size': file_path.stat().st_size,
                'parent_file_id': file_id,
                'extraction_depth': extraction_depth,
            }

            if is_acorn_archive:
                true_name, filetype = parse_acorn_filename(file_path.name)
                if filetype and len(rel_path.parts) > 1:
                    display_path = str(Path(*rel_path.parts[:-1]) / true_name)
                elif filetype:
                    display_path = true_name
                else:
                    display_path = str(rel_path)
                file_entry['path'] = sanitize_path(display_path)
                if filetype:
                    file_entry['risc_os_filetype'] = filetype
            else:
                file_entry['path'] = sanitize_path(str(rel_path))

            files.append(file_entry)

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
                        'file_size': f['size'],
                        'parent_file_id': f['parent_file_id'],
                        'extraction_depth': f['extraction_depth'],
                    }
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

        tool_key = result.get('tool', 'tool').lower().replace(' ', '_')
        po = result.get('process_output')
        details: dict = {
            'file_count': len(files),
            'extraction_depth': extraction_depth,
            'archive_type': archive_type.value,
        }
        if po:
            details[tool_key] = {'process_output': po}

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            tool_name=result['tool'],
            output_path=str(persistent_output),
            summary=f"Extracted {len(files)} files from {archive_info['name']} archive",
            details=json.dumps(details)
        )

    @analysis_handler("disc mastering data detection")
    def process_disc_mastering_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process DISC_MASTERING_DETECT analysis.

        Scans the trailing tracks of an HFE image for mastering/duplicator
        fingerprint data (TRACEBACK format and BCD timestamp record).
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
        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
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
        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            tool_name='hfe_parser',
            summary=summary,
            details=json.dumps(result),
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

        # Mark as running
        self.api.update_analysis(analysis_id, status='running')

        # Create temporary work directory
        with tempfile.TemporaryDirectory(prefix=f'arcology_{analysis_id}_') as work_dir:
            work_path = Path(work_dir)

            try:
                # Dispatch to appropriate handler
                handlers = {
                    AnalysisType.FLUX_VISUALISATION.value: self.process_flux_visualisation,
                    AnalysisType.FLUX_DECODE.value: self.process_flux_decode,
                    AnalysisType.FILE_EXTRACTION.value: self.process_file_extraction,
                    AnalysisType.METADATA_EXTRACT.value: self.process_metadata_extract,
                    AnalysisType.FORMAT_IDENTIFY.value: self.process_format_identify,
                    AnalysisType.PARTITION_DETECT.value: self.process_partition_detect,
                    AnalysisType.ARCHIVE_DETECT.value: self.process_archive_detect,
                    AnalysisType.ARCHIVE_EXTRACT.value: self.process_archive_extract,
                    AnalysisType.DISC_MASTERING_DETECT.value: self.process_disc_mastering_detect,
                    AnalysisType.DISC_PROTECTION_DETECT.value: self.process_disc_protection_detect,
                }

                handler = handlers.get(analysis_type)
                if handler:
                    handler(analysis, artefact, work_path)
                else:
                    log.warning(f"Unknown analysis type: {analysis_type}")
                    self.api.update_analysis(
                        analysis_id,
                        status='failed',
                        error_message=f'Unknown analysis type: {analysis_type}'
                    )

            except Exception as e:
                log.exception(f"Analysis {analysis_id} failed with exception")
                try:
                    self.api.update_analysis(
                        analysis_id,
                        status='failed',
                        error_message=str(e)[:1000]
                    )
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

        while True:
            try:
                processed = self.claim_and_process()

                if processed == 0:
                    log.debug(f"No pending analyses, sleeping {POLL_INTERVAL}s")
                    time.sleep(POLL_INTERVAL)
                else:
                    log.info(f"Processed {processed} analyses")
                    # Small delay between batches
                    time.sleep(1)

            except KeyboardInterrupt:
                log.info("Shutting down")
                break

            except Exception as e:
                log.exception("Unexpected error in main loop")
                time.sleep(POLL_INTERVAL)
