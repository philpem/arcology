"""
API client for communicating with the Arcology server.

Handles all HTTP requests to the REST API including job claiming,
status updates, and artefact registration.
"""

import hashlib
import shutil
from pathlib import Path
import requests
from shared.enums import ArtefactType
from .config import log
from .tools import compute_file_hash


class ArcologyAPI:
    """Client for the Arcology REST API."""

    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path,
                 api_key: str = '', storage=None):
        """
        Initialize the API client.

        Args:
            api_url: Base URL for the Arcology API
            upload_dir: Directory where uploaded files are stored
            output_dir: Directory where derived/output files are stored
            api_key: Worker API key for authentication
            storage: StorageBackend instance for file storage
        """
        self.api = api_url.rstrip('/')
        self.uploads = upload_dir
        self.outputs = output_dir
        self.storage = storage
        self._auth = {'Authorization': f'Bearer {api_key}'} if api_key else {}

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        data: dict | list | None = None,
        expect_json: bool = True,
        log_errors: bool = True,
    ):
        """Perform a worker API request with shared timeout/auth handling."""
        try:
            resp = self._request_response(method, endpoint, data=data)
            resp.raise_for_status()
            if not expect_json:
                return resp
            return resp.json()
        except Exception as exc:
            if log_errors:
                log.error(f"API {method.upper()} {endpoint} failed: {exc}")
            return None

    def _request_response(self, method: str, endpoint: str, *, data: dict | list | None = None):
        """Perform a worker API request and return the raw response object."""
        return requests.request(
            method,
            f"{self.api}{endpoint}",
            json=data,
            headers=self._auth,
            timeout=30,
        )

    def get(self, endpoint: str) -> dict | None:
        """
        GET request to API.

        Args:
            endpoint: API endpoint path (e.g., '/analysis/pending')

        Returns:
            JSON response as dict, or None on error
        """
        return self._request('get', endpoint)

    def put(self, endpoint: str, data: dict) -> dict | None:
        """
        PUT request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        return self._request('put', endpoint, data=data)

    def post(self, endpoint: str, data: dict) -> dict | None:
        """
        POST request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        return self._request('post', endpoint, data=data)

    def patch(self, endpoint: str, data: dict) -> dict | None:
        """
        PATCH request to API.

        Args:
            endpoint: API endpoint path
            data: JSON data to send

        Returns:
            JSON response as dict, or None on error
        """
        return self._request('patch', endpoint, data=data)

    def run_hash_rescan(self, artefact_uuid: str) -> dict | None:
        """Trigger a server-side hash rescan for one artefact.

        Returns the result dict {updated, total, recognition_queued}, or None
        on network/server error.
        """
        resp = self._request('post', f'/artefact/{artefact_uuid}/hash-rescan')
        if resp is None or not resp.ok:
            log.error('run_hash_rescan(%s): HTTP %s', artefact_uuid,
                      resp.status_code if resp else 'no response')
            return None
        return resp.json()

    def update_artefact_hashes(self, artefact_uuid: str, md5: str, sha256: str):
        """Write computed MD5 and SHA256 hashes back to the artefact record."""
        self.patch(f"/artefacts/{artefact_uuid}", {'md5': md5, 'sha256': sha256})

    def update_artefact_media_metadata(self, artefact_uuid: str, metadata: dict):
        """
        Write format-specific metadata back to the artefact record.

        The server merges the supplied dict into any existing media_metadata
        JSON so callers can populate one section at a time (e.g. just
        ``iso9660``) without clobbering others.
        """
        self.patch(f"/artefacts/{artefact_uuid}", {'media_metadata': metadata})

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
            resp = self._request_response('put', f'/analysis/{analysis_id}', data=kwargs)
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
            ) from e
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
        auto_analyse: bool = True,
        skip_analyses: list[str] | None = None,
    ) -> dict | None:
        """
        Register a derived artefact produced by an analysis.
        Stores file via storage backend and calls API.

        Args:
            analysis_id: ID of the analysis that produced this artefact
            label: Human-readable label for the artefact
            source_path: Path to the generated file
            artefact_type: Type of the artefact
            auto_analyse: Whether to auto-queue follow-on analyses (default True).
                Set to False when the caller will explicitly queue specific analyses.
            skip_analyses: List of AnalysisType values (strings) to suppress when
                auto-queuing follow-on analyses.  Used to prevent ping-pong: e.g.
                HFE/IMD siblings produced by FLUX_DECODE carry skip_analyses=['FLUX_DECODE']
                so they don't re-trigger the decode.

        Returns:
            API response dict, or None on error
        """
        # Build a deterministic storage name so that retries after an API
        # failure reuse the same key rather than uploading a second copy and
        # leaving the first orphaned in S3.  The combination of analysis_id
        # and source filename is unique within the system.
        name_hash = hashlib.sha256(
            f"{analysis_id}:{source_path.stem}".encode()
        ).hexdigest()[:24]
        storage_name = f"{name_hash}{source_path.suffix}"

        # Store file via storage backend
        if self.storage:
            key = self.storage.storage_key('outputs', storage_name)
            self.storage.put(key, source_path)
            # Compute hashes from source (already local)
            md5, sha256, file_size = compute_file_hash(source_path)
        else:
            # Fallback: direct copy (legacy path without storage backend)
            storage_path = self.outputs / storage_name
            shutil.copy(source_path, storage_path)
            md5, sha256, file_size = compute_file_hash(storage_path)

        payload = {
            'label': label,
            'original_filename': source_path.name,
            'storage_path': storage_name,
            'storage_directory': 'outputs',
            'artefact_type': artefact_type.value,
            'file_size': file_size,
            'md5': md5,
            'sha256': sha256,
            'auto_analyse': auto_analyse,
        }
        if skip_analyses:
            payload['skip_analyses'] = skip_analyses

        # Register via API - derived artefacts use 'outputs' storage directory
        return self.post(f"/analysis/{analysis_id}/produce-artefact", payload)

    def register_file_listing(
        self,
        artefact_uuid: str,
        files: list[dict],
        filesystem: str = 'unknown',
        label: str = None,
        container_format: str = None,
        partition_index: int = 0,
        archive_comment: str | None = None,
    ):
        """
        Register extracted file listing in API.

        Args:
            artefact_uuid: UUID of the artefact containing the files
            files: List of file dicts with path, size, md5, sha1, sha256, etc.
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
        if archive_comment:
            partition_data['archive_comment'] = archive_comment

        partition_resp = self.post(f"/artefacts/{artefact_uuid}/partitions", partition_data)

        if not partition_resp:
            log.error("Failed to create partition")
            return None

        partition_uuid = partition_resp.get('uuid')

        self.post_file_records(partition_uuid, files)

        return partition_resp

    def post_file_records(
        self,
        partition_uuid: str,
        files: list[dict],
        batch_size: int = 100,
    ) -> int:
        """
        Convert a file list from enumerate_extracted_files() into API records
        and POST them to /partitions/{partition_uuid}/files in batches.

        Args:
            partition_uuid: UUID of the target partition (must already exist).
            files: List of file dicts as returned by enumerate_extracted_files().
            batch_size: Number of records per POST request (default 100).

        Returns:
            Total number of file records submitted.
        """
        total = 0
        for i in range(0, len(files), batch_size):
            batch = files[i:i + batch_size]
            file_records = []
            for f in batch:
                path = f.get('path', '')
                file_records.append({
                    'path': path,
                    'filename': Path(path).name,
                    'extension': Path(path).suffix.lstrip('.').lower() or None,
                    'file_size': f.get('size'),
                    'modified_time': f.get('modified_time'),
                    'md5': f.get('md5'),
                    'sha1': f.get('sha1'),
                    'sha256': f.get('sha256'),
                    'is_directory': f.get('is_directory', False),
                    'risc_os_filetype': f.get('risc_os_filetype'),
                    'load_address': f.get('load_address'),
                    'exec_address': f.get('exec_address'),
                    'attributes': f.get('attributes'),
                    'parent_file_id': f.get('parent_file_id'),
                    'extraction_depth': f.get('extraction_depth', 0),
                })
            self.post(f"/partitions/{partition_uuid}/files", {'files': file_records})
            total += len(batch)
        return total

    def queue_analysis(
        self,
        artefact_uuid: str,
        analysis_type: str,
        hints: dict = None
    ) -> dict | None:
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

    def get_pending_analyses(self, analysis_types: list[str] | None = None) -> list[dict]:
        """
        Get list of pending analysis jobs.

        Args:
            analysis_types: Optional list of AnalysisType names to restrict to.
                            When provided, only jobs of those types are returned.

        Returns:
            List of analysis dicts, or empty list on error
        """
        url = '/analysis/pending'
        if analysis_types:
            url += '?types=' + ','.join(analysis_types)
        response = self.get(url)
        if not response:
            return []
        return response.get('analyses', [])

    def reset_stale_analyses(self) -> int:
        """
        Ask the server to reset any RUNNING jobs older than the stale timeout
        back to PENDING.  Called on worker startup to recover from a previous crash.

        Returns:
            Number of jobs reset, or 0 on error.
        """
        try:
            resp = self._request_response('post', '/analysis/reset-stale', data={})
            resp.raise_for_status()
            count = resp.json().get('reset', 0)
            if count:
                log.info(f'Recovered {count} stale analysis job(s) left by a previous worker crash')
            return count
        except Exception as e:
            log.warning(f'Failed to reset stale analyses on startup: {e}')
            return 0

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
            resp = self._request_response(
                'put',
                f'/analysis/{analysis_id}',
                data={'status': 'running', 'claim_worker': True},
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
