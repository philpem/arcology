"""
API client for communicating with the Arcology server.

Handles all HTTP requests to the REST API including job claiming,
status updates, and artefact registration.
"""

import shutil
import uuid
from pathlib import Path
from typing import Optional

import requests

from .config import log
from shared.enums import ArtefactType
from .tools import compute_file_hash


class ArcologyAPI:
    """Client for the Arcology REST API."""

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path, api_key: str = ''):
        """
        Initialize the API client.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded files are stored
            output_dir: Directory where derived/output files are stored
            api_key: Worker API key for authentication
        """
        self.api = api_url.rstrip('/')
        self.uploads = upload_dir
        self.outputs = output_dir
        self._auth = {'Authorization': f'Bearer {api_key}'} if api_key else {}

    def get(self, endpoint: str) -> Optional[dict]:
        """
        GET request to API.

        Args:
            endpoint: API endpoint path (e.g., '/analysis/pending')

        Returns:
            JSON response as dict, or None on error
        """
        try:
            resp = requests.get(f"{self.api}{endpoint}", headers=self._auth, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API GET {endpoint} failed: {e}")
            return None

    def put(self, endpoint: str, data: dict) -> Optional[dict]:
        """
        PUT request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        try:
            resp = requests.put(
                f"{self.api}{endpoint}",
                json=data,
                headers=self._auth,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API PUT {endpoint} failed: {e}")
            return None

    def post(self, endpoint: str, data: dict) -> Optional[dict]:
        """
        POST request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        try:
            resp = requests.post(
                f"{self.api}{endpoint}",
                json=data,
                headers=self._auth,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API POST {endpoint} failed: {e}")
            return None

    def patch(self, endpoint: str, data: dict) -> Optional[dict]:
        """
        PATCH request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        try:
            resp = requests.patch(
                f"{self.api}{endpoint}",
                json=data,
                headers=self._auth,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API PATCH {endpoint} failed: {e}")
            return None

    def update_artefact_hashes(self, artefact_uuid: str, md5: str, sha256: str):
        """Write computed MD5 and SHA256 hashes back to the artefact record."""
        self.patch(f"/artefacts/{artefact_uuid}", {'md5': md5, 'sha256': sha256})

    def update_analysis(self, analysis_id: int, **kwargs):
        """
        Update analysis record in API.

        Args:
            analysis_id: ID of the analysis to update
            **kwargs: Fields to update (status, success, error_message, etc.)

        Raises:
            RuntimeError: If the API call fails (e.g. network error, server
                error, or the server rejected the data).  Callers that want
                the job to be marked as failed rather than left stuck in
                'running' state should let this propagate to the
                analysis_handler decorator, which will catch it and attempt
                a minimal failure report.
        """
        try:
            resp = requests.put(
                f"{self.api}/analysis/{analysis_id}",
                json=kwargs,
                headers=self._auth,
                timeout=30
            )
            if resp.status_code == 404:
                log.warning(
                    f"Analysis {analysis_id} no longer exists on the server "
                    f"(it was probably deleted by a re-analyse). Discarding result."
                )
                return
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"update_analysis failed for analysis {analysis_id}: {e}"
            )
        except Exception as e:
            raise RuntimeError(
                f"update_analysis failed for analysis {analysis_id} "
                f"(API returned no response — see worker log for details)"
            ) from e

    def register_derived_artefact(
        self,
        analysis_id: int,
        label: str,
        source_path: Path,
        artefact_type: ArtefactType,
        auto_analyse: bool = True
    ) -> Optional[dict]:
        """
        Register a derived artefact produced by an analysis.
        Copies file to outputs directory and calls API.

        Args:
            analysis_id: ID of the analysis that produced this artefact
            label: Human-readable label for the artefact
            source_path: Path to the generated file
            artefact_type: Type of the artefact
            auto_analyse: Whether to auto-queue follow-on analyses (default True).
                Set to False when the caller will explicitly queue specific analyses.

        Returns:
            API response dict, or None on error
        """
        storage_name = f"{uuid.uuid4().hex}{source_path.suffix}"
        storage_path = self.outputs / storage_name

        # Copy file to outputs (derived files go there, not uploads)
        shutil.copy(source_path, storage_path)

        # Compute hashes
        md5, sha256, file_size = compute_file_hash(storage_path)

        # Register via API - derived artefacts use 'outputs' storage directory
        return self.post(f"/analysis/{analysis_id}/produce-artefact", {
            'label': label,
            'original_filename': source_path.name,
            'storage_path': storage_name,
            'storage_directory': 'outputs',
            'artefact_type': artefact_type.value,
            'file_size': file_size,
            'md5': md5,
            'sha256': sha256,
            'auto_analyse': auto_analyse
        })

    def register_file_listing(
        self,
        artefact_uuid: str,
        files: list[dict],
        filesystem: str = 'unknown',
        label: str = None,
        container_format: str = None,
        partition_index: int = 0,
    ):
        """
        Register extracted file listing in API.

        Args:
            artefact_uuid: UUID of the artefact containing the files
            files: List of file dicts with path, size, crc32, etc.
            filesystem: Filesystem type (e.g., 'fat', 'adfs')
            label: Optional partition label (e.g., disc name for ADFS)
            container_format: Optional detailed format info (e.g., "Acorn ADFS E")
            partition_index: Partition index within the parent disc image

        Returns:
            Partition dict if successful, None otherwise
        """
        # First create partition
        partition_data = {
            'partition_index': partition_index,
            'filesystem': filesystem,
            'total_files': len(files)
        }
        if label:
            partition_data['label'] = label
        if container_format:
            partition_data['container_format'] = container_format

        partition_resp = self.post(f"/artefacts/{artefact_uuid}/partitions", partition_data)

        if not partition_resp:
            log.error("Failed to create partition")
            return None

        partition_uuid = partition_resp.get('uuid')

        # Add files in batches
        batch_size = 100
        for i in range(0, len(files), batch_size):
            batch = files[i:i+batch_size]
            file_records = []
            for f in batch:
                path = f.get('path', '')
                file_records.append({
                    'path': path,
                    'filename': Path(path).name,
                    'extension': Path(path).suffix.lstrip('.').lower() or None,
                    'file_size': f.get('size'),
                    'crc32': f.get('crc32'),
                    'md5': f.get('md5'),
                    'sha1': f.get('sha1'),
                    'sha256': f.get('sha256'),
                    # Archive support fields
                    'risc_os_filetype': f.get('risc_os_filetype'),
                    'parent_file_id': f.get('parent_file_id'),
                    'extraction_depth': f.get('extraction_depth', 0)
                })

            self.post(f"/partitions/{partition_uuid}/files", {'files': file_records})

        return partition_resp

    def queue_analysis(
        self,
        artefact_uuid: str,
        analysis_type: str,
        hints: dict = None
    ) -> Optional[dict]:
        """
        Queue a new analysis for an artefact.

        Args:
            artefact_uuid: UUID of the artefact to analyze
            analysis_type: Type of analysis (e.g., 'archive_detect')
            hints: Optional hints dict (will be JSON-encoded)

        Returns:
            Analysis dict if successful, None otherwise
        """
        import json
        data = {'analysis_type': analysis_type}
        if hints:
            data['hints'] = json.dumps(hints)

        return self.post(f"/artefacts/{artefact_uuid}/analysis", data)

    def get_pending_analyses(self) -> list[dict]:
        """
        Get list of pending analysis jobs.

        Returns:
            List of analysis dicts, or empty list on error
        """
        response = self.get('/analysis/pending')
        if not response:
            return []
        return response.get('analyses', [])

    def claim_analysis(self, analysis_id: int) -> bool:
        """
        Attempt to claim an analysis job for processing.

        Uses atomic database-level claiming to prevent race conditions
        when multiple workers try to claim the same job.

        Returns False (instead of logging an error) when the analysis no
        longer exists — this happens normally when an item is deleted while
        its analyses are still pending.

        Args:
            analysis_id: ID of the analysis to claim

        Returns:
            True if successfully claimed, False otherwise
        """
        try:
            resp = requests.put(
                f"{self.api}/analysis/{analysis_id}",
                json={'status': 'running', 'claim_worker': True},
                headers=self._auth,
                timeout=30
            )
            if resp.status_code == 404:
                log.debug(
                    f"Analysis {analysis_id} no longer exists (deleted), skipping"
                )
                return False
            resp.raise_for_status()
            return resp.json().get('claimed', False)
        except Exception as e:
            log.error(f"Failed to claim analysis {analysis_id}: {e}")
            return False

    def get_recognition_config(self) -> list[dict]:
        """
        Fetch hash databases with product recognition enabled, with full product/file data.

        Returns:
            List of database dicts, or empty list on error
        """
        response = self.get('/hash-databases/recognition-config')
        if not response:
            return []
        return response if isinstance(response, list) else []

    def report_recognised_products(self, partition_uuid: str, results: list[dict]) -> bool:
        """
        Report product recognition results for a partition.

        Args:
            partition_uuid: UUID of the partition
            results: List of match dicts with product_id, folder_path, counts

        Returns:
            True on success, False otherwise
        """
        response = self.post(f'/partitions/{partition_uuid}/recognised-products', results)
        return response is not None

# vim: ts=4 sw=4 et
