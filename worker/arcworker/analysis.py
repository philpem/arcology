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
        artefact_id = artefact['id']
        input_path = self.get_input_path(artefact, work_dir)

        outputs = []

        # Try Fluxfox first (more detailed)
        output_fluxfox = work_dir / f"{artefact_id}_fluxfox.png"
        result_fluxfox = flux_visualisation_fluxfox(input_path, output_fluxfox)

        if result_fluxfox['success']:
            saved_name = self.save_output_file(output_fluxfox, f"{artefact_id}_fluxfox.png")
            outputs.append({
                'tool': 'fluxfox',
                'type': 'image',
                'filename': saved_name,
                'description': 'Fluxfox visualisation'
            })

        # Also generate HxCFE visualisation (different style)
        output_hxcfe = work_dir / f"{artefact_id}_hxcfe.png"
        result_hxcfe = flux_visualisation_hxcfe(input_path, output_hxcfe)

        if result_hxcfe['success']:
            saved_name = self.save_output_file(output_hxcfe, f"{artefact_id}_hxcfe.png")
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
        """
        analysis_id = analysis['id']
        artefact_id = artefact['id']
        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')

        # Try 7z first (works for most formats)
        result = list_files_7z(input_path)

        if result['success'] and result['files']:
            # Determine filesystem type from hints or detection
            filesystem = hints.get('filesystem', 'unknown')

            self.api.register_file_listing(artefact_id, result['files'], filesystem)

            self.api.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name=result['tool'],
                summary=result['summary'],
                details=json.dumps({'file_count': result['file_count']})
            )
        else:
            self.api.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=result.get('error', 'Could not list files')
            )

    def process_file_extraction(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_EXTRACTION analysis.
        Extracts files based on detected/hinted filesystem type.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')

        filesystem = hints.get('filesystem', '').lower()
        extract_dir = work_dir / 'extracted'

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
                error_message=result.get('error', 'Extraction failed')
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
