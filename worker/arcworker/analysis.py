"""
Analysis worker and job processing.

Contains the main AnalysisWorker class that polls for jobs and
dispatches them to appropriate handlers.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

from .config import log, POLL_INTERVAL
from .types import ArtefactType, AnalysisType
from .compression import decompress_if_needed
from .api import ArcologyAPI
from .tools import (
    compute_file_hash,
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    flux_to_imd_hxcfe,
    flux_to_hfe_hxcfe,
    sector_image_to_raw_greaseweazle,
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    list_files_7z,
    list_files_dim,
    detect_partitions_sfdisk,
    detect_acorn_adfs,
    detect_format_file_cmd,
)


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

    def get_input_path(self, artefact: dict, work_dir: Path) -> Path:
        """
        Get input file path, decompressing if needed.

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

        return decompress_if_needed(input_path, work_dir)

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

    def process_flux_decode(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FLUX_DECODE analysis.
        Attempts to decode flux to sector image, producing derived artefacts.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        artefact_label = artefact['label']

        results = []

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
                ArtefactType.IMG
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

    def process_file_listing(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_LISTING analysis.
        Lists files in sector image without extracting.
        Only works on raw sector images (IMG) - not HFE or IMD formats.
        """
        analysis_id = analysis['id']
        artefact_id = artefact['id']
        artefact_type = artefact.get('artefact_type', '')

        # Only raw sector images can be processed by 7z and DIM
        # HFE is an emulator container format, IMD is track-based with metadata
        # These need to be converted to IMG first via flux_decode
        supported_types = (
            ArtefactType.IMG.value,
            ArtefactType.ISO.value,
            ArtefactType.DD.value,
            ArtefactType.DD_ZST.value,
            ArtefactType.DD_GZ.value,
            ArtefactType.DD_BZ2.value,
        )
        if artefact_type not in supported_types:
            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=False,
                error_message=f'File listing not supported for {artefact_type} format. Only raw sector images are supported.'
            )
            return

        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')

        filesystem = hints.get('filesystem', '').lower()

        # Choose listing method based on filesystem hint
        if filesystem in ('dfs', 'adfs', 'acorn'):
            result = list_files_dim(input_path)
        elif filesystem in ('fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos'):
            result = list_files_7z(input_path)
        else:
            # Try 7z as default (handles many formats)
            result = list_files_7z(input_path)

            # If that fails and no filesystem hint, try Acorn
            if not (result['success'] and result.get('files')) and not filesystem:
                result = list_files_dim(input_path)

        if result['success'] and result.get('files'):
            # Use filesystem from hints or 'unknown'
            fs_type = filesystem if filesystem else 'unknown'

            partition = self.api.register_file_listing(artefact_id, result['files'], fs_type)

            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name=result['tool'],
                summary=result['summary'],
                details=json.dumps({'file_count': result['file_count']})
            )

            # Queue ARCHIVE_DETECT to scan for archives in extracted files
            if partition:
                from .types import AnalysisType
                self.api.queue_analysis(
                    artefact['uuid'],
                    AnalysisType.ARCHIVE_DETECT.value,
                    hints={'partition_id': partition.get('id')}
                )
        else:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                tool_name=result.get('tool'),
                error_message=result.get('error', 'Could not list files'),
                details=json.dumps({
                    'process_output': result.get('process_output')
                })
            )

    def process_file_extraction(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_EXTRACTION analysis.
        Extracts files based on detected/hinted filesystem type.
        Only works on raw sector images (IMG) - not HFE or IMD formats.

        Files are extracted to persistent hierarchical storage.
        """
        from .utils.paths import get_output_path
        from .config import OUTPUT_DIR

        analysis_id = analysis['id']
        artefact_id = artefact['id']
        artefact_type = artefact.get('artefact_type', '')

        # Only raw sector images can be processed by 7z and DIM
        # HFE is an emulator container format, IMD is track-based with metadata
        # These need to be converted to IMG first via flux_decode
        supported_types = (
            ArtefactType.IMG.value,
            ArtefactType.ISO.value,
            ArtefactType.DD.value,
            ArtefactType.DD_ZST.value,
            ArtefactType.DD_GZ.value,
            ArtefactType.DD_BZ2.value,
        )
        if artefact_type not in supported_types:
            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=False,
                error_message=f'File extraction not supported for {artefact_type} format. Only raw sector images are supported.'
            )
            return

        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')
        filesystem = hints.get('filesystem', '').lower()

        # Get Item for hierarchical path (artefact should have item reference)
        # For now, create a simple structure - can be enhanced later
        item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

        # Use hierarchical output path for persistent storage
        extract_dir = get_output_path(
            OUTPUT_DIR,
            item,
            artefact,
            analysis,
            partition=None  # Will be set after partition is created
        )

        # Choose extraction method based on filesystem
        if filesystem in ('dfs', 'adfs', 'acorn'):
            result = extract_acorn_disc_image_manager(input_path, extract_dir)
        elif filesystem in ('fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos'):
            result = extract_dos_7z(input_path, extract_dir)
        else:
            # Try 7z as default (handles many formats)
            result = extract_dos_7z(input_path, extract_dir)

            # If that fails and no filesystem hint, try Acorn
            if not result['success'] and not filesystem:
                result = extract_acorn_disc_image_manager(input_path, extract_dir)

        if result['success']:
            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name=result['tool'],
                summary=result['summary'],
                output_path=str(extract_dir),
                details=json.dumps({'file_count': result.get('file_count', 0)})
            )
        else:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                tool_name=result.get('tool'),
                error_message=result.get('error', 'Extraction failed'),
                details=json.dumps({
                    'process_output': result.get('process_output')
                })
            )

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

    def process_format_identify(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FORMAT_IDENTIFY analysis.
        Attempts to identify the exact format of an image.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)

        # Placeholder - format identification not yet implemented
        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            summary='Format identification not yet implemented',
            details=json.dumps({'detected': 'unknown'})
        )

    def process_partition_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process PARTITION_DETECT analysis.
        Detects partitions and filesystem types in raw disc images.
        Checks for Acorn ADFS (Filecore) first -- a reformatted PC disc
        may retain a stale MBR, so sfdisk is only used when no ADFS
        signatures are found.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')
        filesystem_hint = hints.get('filesystem', '').lower()

        results = {}
        detected_partitions = []

        # 1. Check for Acorn ADFS (Filecore) first.
        # Filecore writes from byte 0xC00 onwards, so a disc reformatted
        # from PC to Filecore may retain a stale MBR in the first 5 sectors.
        # If we detect ADFS, skip sfdisk to avoid reporting that stale MBR.
        adfs_result = detect_acorn_adfs(input_path)
        results['adfs'] = adfs_result

        if adfs_result.get('adfs_detected'):
            detected_partitions = [{
                'index': 0,
                'filesystem': 'adfs',
                'description': f'Acorn ADFS ({adfs_result.get("adfs_variant", "unknown variant")})',
                'size_bytes': adfs_result.get('disc_size'),
                'signatures': adfs_result.get('signatures', []),
            }]

        # 2. If no ADFS detected, try sfdisk for standard partition tables (MBR/GPT)
        if not detected_partitions:
            sfdisk_result = detect_partitions_sfdisk(input_path)
            results['sfdisk'] = sfdisk_result

            if sfdisk_result['success']:
                detected_partitions = sfdisk_result['partitions']

        # 3. Use file command for additional format info
        file_result = detect_format_file_cmd(input_path)
        results['file'] = file_result

        # 4. If nothing detected, report whole disc as single unknown partition
        if not detected_partitions:
            file_size = input_path.stat().st_size
            detected_partitions = [{
                'index': 0,
                'filesystem': filesystem_hint or 'unknown',
                'description': 'No partition table detected (whole disc)',
                'size_bytes': file_size,
            }]

        # Build summary
        fs_types = [p.get('filesystem', 'unknown') for p in detected_partitions]
        summary = f'Detected {len(detected_partitions)} partition(s): {", ".join(fs_types)}'

        if adfs_result.get('adfs_detected'):
            summary += f' (ADFS signatures: {", ".join(adfs_result.get("signatures", []))})'

        if file_result.get('file_type'):
            summary += f' [file: {file_result["file_type"][:200]}]'

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            tool_name='sfdisk,adfs_detect,file',
            summary=summary,
            details=json.dumps({
                'partitions': detected_partitions,
                'results': results
            })
        )

    def process_archive_detect(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process ARCHIVE_DETECT analysis.
        Scans partition files for archives and queues extraction jobs.
        """
        import json
        from myapp.archive_formats import (
            get_archive_by_filetype,
            get_archive_by_extension,
            get_archive_info,
            is_compressor_format,
        )
        from .config import MAX_ARCHIVE_DEPTH

        analysis_id = analysis['id']
        hints = json.loads(analysis.get('hints') or '{}')
        partition_id = hints.get('partition_id')

        if not partition_id:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='No partition_id in analysis hints'
            )
            return

        # Get all files in partition from API
        partition_resp = self.api.get(f"/partitions/{partition_id}/files?per_page=10000")
        if not partition_resp:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Failed to get partition files'
            )
            return

        files = partition_resp.get('files', [])

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
            self.api.queue_analysis(
                artefact['uuid'],
                AnalysisType.ARCHIVE_EXTRACT.value,
                hints={
                    'file_id': file_data['id'],
                    'partition_id': partition_id,
                    'archive_type': archive_type.value,
                    'archive_format': archive_info['name'],
                    'is_compressor': is_compressor,
                    'extraction_depth': current_depth + 1
                }
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

    def process_archive_extract(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process ARCHIVE_EXTRACT analysis.
        Extracts a specific archive file and registers the extracted files.

        Performs actual extraction with file path resolution.
        """
        import json
        from myapp.archive_formats import (
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
            list_files_dim,
        )
        from .tools.extraction import convert_fcfs_to_raw
        from .config import OUTPUT_DIR, MAX_ARCHIVE_DEPTH
        from .utils.paths import get_output_path

        analysis_id = analysis['id']
        hints = json.loads(analysis.get('hints') or '{}')

        file_id = hints.get('file_id')
        partition_id = hints.get('partition_id')
        archive_type_str = hints.get('archive_type')
        is_compressor = hints.get('is_compressor', False)
        extraction_depth = hints.get('extraction_depth', 1)

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
        partition_resp = self.api.get(f"/partitions/{partition_id}")
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
        files_resp = self.api.get(f"/partitions/{partition_id}/files?per_page=10000")
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

        # Get the partition's parent artefact analyses to find extraction output_path
        artefact_uuid = artefact.get('uuid')
        analyses_resp = self.api.get(f"/artefacts/{artefact_uuid}/analysis")

        # Find FILE_EXTRACTION, FILE_LISTING, or ARCHIVE_EXTRACT analysis for parent files
        extraction_path = None
        for a in analyses_resp.get('analyses', []):
            if a.get('analysis_type') in ['file_extraction', 'file_listing', 'archive_extract']:
                extraction_path = a.get('output_path')
                if extraction_path:
                    break

        if not extraction_path:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message='Could not determine extraction path for files'
            )
            return

        # Construct full path to archive file
        archive_path = Path(extraction_path) / target_file['path']

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

        elif archive_type == ArchiveType.ZIP:
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

        # Scan extracted files from persistent storage
        files = []
        for file_path in persistent_output.rglob('*'):
            if not file_path.is_file():
                continue

            rel_path = file_path.relative_to(persistent_output)
            files.append({
                'path': str(rel_path),
                'size': file_path.stat().st_size,
                'parent_file_id': file_id,
                'extraction_depth': extraction_depth
            })

        # Register extracted files in the same partition with parent_file_id
        if files:
            # Register files (they'll be added to the same partition)
            for i in range(0, len(files), 100):
                batch = files[i:i+100]
                file_records = []
                for f in batch:
                    file_records.append({
                        'path': f['path'],
                        'filename': Path(f['path']).name,
                        'extension': Path(f['path']).suffix.lstrip('.').lower() or None,
                        'file_size': f['size'],
                        'parent_file_id': f['parent_file_id'],
                        'extraction_depth': f['extraction_depth']
                    })
                self.api.post(f"/partitions/{partition_id}/files", {'files': file_records})

        # Queue ARCHIVE_DETECT for nested archives (if under depth limit)
        if extraction_depth < MAX_ARCHIVE_DEPTH:
            self.api.queue_analysis(
                artefact['uuid'],
                AnalysisType.ARCHIVE_DETECT.value,
                hints={'partition_id': partition_id}
            )

        self.api.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            tool_name=result['tool'],
            output_path=str(persistent_output),
            summary=f"Extracted {len(files)} files from {archive_info['name']} archive",
            details=json.dumps({
                'file_count': len(files),
                'extraction_depth': extraction_depth,
                'archive_type': archive_type.value
            })
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
                    AnalysisType.FILE_LISTING.value: self.process_file_listing,
                    AnalysisType.FILE_EXTRACTION.value: self.process_file_extraction,
                    AnalysisType.METADATA_EXTRACT.value: self.process_metadata_extract,
                    AnalysisType.FORMAT_IDENTIFY.value: self.process_format_identify,
                    AnalysisType.PARTITION_DETECT.value: self.process_partition_detect,
                    AnalysisType.ARCHIVE_DETECT.value: self.process_archive_detect,
                    AnalysisType.ARCHIVE_EXTRACT.value: self.process_archive_extract,
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
                self.api.update_analysis(
                    analysis_id,
                    status='failed',
                    error_message=str(e)[:1000]
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
