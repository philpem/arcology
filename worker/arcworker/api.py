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
from .types import ArtefactType
from .tools import compute_file_hash


class ArcologyAPI:
    """Client for the Arcology REST API."""

    def __init__(self, api_url: str, upload_dir: Path):
        """
        Initialize the API client.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded files are stored
        """
        self.api = api_url.rstrip('/')
        self.uploads = upload_dir

    def get(self, endpoint: str) -> Optional[dict]:
        """
        GET request to API.

        Args:
            endpoint: API endpoint path (e.g., '/analysis/pending')

        Returns:
            JSON response as dict, or None on error
        """
        try:
            resp = requests.get(f"{self.api}{endpoint}", timeout=30)
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
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API POST {endpoint} failed: {e}")
            return None

    def update_analysis(self, analysis_id: int, **kwargs):
        """
        Update analysis record in API.

        Args:
            analysis_id: ID of the analysis to update
            **kwargs: Fields to update (status, success, error_message, etc.)
        """
        self.put(f"/analysis/{analysis_id}", kwargs)

    def register_derived_artefact(
        self,
        analysis_id: int,
        label: str,
        source_path: Path,
        artefact_type: ArtefactType
    ) -> Optional[dict]:
        """
        Register a derived artefact produced by an analysis.
        Copies file to uploads directory and calls API.

        Args:
            analysis_id: ID of the analysis that produced this artefact
            label: Human-readable label for the artefact
            source_path: Path to the generated file
            artefact_type: Type of the artefact

        Returns:
            API response dict, or None on error
        """
        storage_name = f"{uuid.uuid4().hex}{source_path.suffix}"
        storage_path = self.uploads / storage_name

        # Copy file to uploads
        shutil.copy(source_path, storage_path)

        # Compute hashes
        md5, sha256, file_size = compute_file_hash(storage_path)

        # Register via API
        return self.post(f"/analysis/{analysis_id}/produce-artefact", {
            'label': label,
            'original_filename': source_path.name,
            'storage_path': storage_name,
            'artefact_type': artefact_type.value,
            'file_size': file_size,
            'md5': md5,
            'sha256': sha256
        })

    def register_file_listing(
        self,
        artefact_id: int,
        files: list[dict],
        filesystem: str = 'unknown'
    ):
        """
        Register extracted file listing in API.

        Args:
            artefact_id: ID of the artefact containing the files
            files: List of file dicts with path, size, crc32, etc.
            filesystem: Filesystem type (e.g., 'fat', 'adfs')
        """
        # First create partition
        partition_resp = self.post(f"/artefacts/{artefact_id}/partitions", {
            'partition_index': 0,
            'filesystem': filesystem,
            'total_files': len(files)
        })

        if not partition_resp:
            log.error("Failed to create partition")
            return

        partition_id = partition_resp.get('id')

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
                    'sha1': f.get('sha1')
                })

            self.post(f"/partitions/{partition_id}/files", {'files': file_records})

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

        Args:
            analysis_id: ID of the analysis to claim

        Returns:
            True if successfully claimed, False otherwise
        """
        claim_result = self.put(f"/analysis/{analysis_id}", {
            'status': 'running',
            'claim_worker': True  # Signal this is a claim attempt
        })

        return claim_result and claim_result.get('status') == 'running'
