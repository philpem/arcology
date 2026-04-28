"""
HTTP client for the Arcology REST API.

Modelled on worker/arcworker/api.py but adapted for CLI use:
- Uses requests.Session for connection reuse
- Raises exceptions on errors instead of returning None
- Supports multipart file upload and streaming download

This is the canonical Python client for the Arcology API, used by the
``arco`` CLI and available for third-party scripts::

    from arccli.client import ArcologyClient
    client = ArcologyClient("http://localhost:5000", "my-api-key")
    items = client.list_items_all(tag="retro")
"""

import hashlib
import json
import logging
import math
import os
import time

import requests

# Files larger than this threshold are uploaded in chunks
CHUNKED_THRESHOLD = 100 * 1024 * 1024   # 100 MB
CHUNK_SIZE        =  50 * 1024 * 1024   #  50 MB

log = logging.getLogger(__name__)


class ArcologyError(Exception):
	"""Base exception for Arcology API errors."""
	def __init__(self, message, status_code=None, response=None):
		super().__init__(message)
		self.status_code = status_code
		self.response = response


class ArcologyClient:
	"""HTTP client for the Arcology REST API."""

	def __init__(self, base_url: str, api_key: str):
		self.base_url = base_url.rstrip('/')
		self.api_url = f"{self.base_url}/api"
		self.session = requests.Session()
		self.session.headers['Authorization'] = f'Bearer {api_key}'

	def _url(self, endpoint: str) -> str:
		return f"{self.api_url}/{endpoint.lstrip('/')}"

	def _handle_response(self, resp: requests.Response) -> dict:
		"""Check response status and return JSON, or raise ArcologyError."""
		if resp.status_code == 401:
			raise ArcologyError("Authentication failed. Check your API key.", 401, resp)
		if resp.status_code == 403:
			raise ArcologyError("Insufficient permissions.", 403, resp)
		if resp.status_code == 404:
			raise ArcologyError("Not found.", 404, resp)
		if resp.status_code == 409:
			# Duplicate artefact — return the existing record, not an error
			result = resp.json()
			result['duplicate'] = True
			return result
		if resp.status_code == 413:
			raise ArcologyError("File too large for server.", 413, resp)
		if resp.status_code >= 400:
			try:
				detail = resp.json().get('error', resp.text)
			except Exception:
				detail = resp.text
			raise ArcologyError(f"API error ({resp.status_code}): {detail}", resp.status_code, resp)
		if resp.status_code == 204:
			return {}
		return resp.json()

	def get(self, endpoint: str, params: dict = None) -> dict:
		"""GET request to API endpoint."""
		resp = self.session.get(self._url(endpoint), params=params)
		return self._handle_response(resp)

	def post_json(self, endpoint: str, data: dict) -> dict:
		"""POST JSON data to API endpoint."""
		resp = self.session.post(self._url(endpoint), json=data)
		return self._handle_response(resp)

	def post_file(self, endpoint: str, filepath: str, fields: dict) -> dict:
		"""POST multipart file upload to API endpoint."""
		with open(filepath, 'rb') as f:
			files = {'file': (os.path.basename(filepath), f)}
			resp = self.session.post(self._url(endpoint), files=files, data=fields)
		return self._handle_response(resp)

	def put(self, endpoint: str, data: dict) -> dict:
		"""PUT JSON data to API endpoint."""
		resp = self.session.put(self._url(endpoint), json=data)
		return self._handle_response(resp)

	def delete(self, endpoint: str) -> dict:
		"""DELETE request to API endpoint."""
		resp = self.session.delete(self._url(endpoint))
		return self._handle_response(resp)

	def download(self, endpoint: str, output_path: str):
		"""Download a file from API endpoint to local path."""
		resp = self.session.get(self._url(endpoint), stream=True)
		if resp.status_code >= 400:
			self._handle_response(resp)
		with open(output_path, 'wb') as f:
			for chunk in resp.iter_content(chunk_size=8192):
				f.write(chunk)

	# ---- Convenience methods ----

	def health(self) -> dict:
		"""Check server health (no auth required)."""
		resp = self.session.get(f"{self.api_url}/health")
		return self._handle_response(resp)

	def list_items(self, **params) -> dict:
		return self.get('items', params={k: v for k, v in params.items() if v is not None})

	def get_item(self, uuid: str) -> dict:
		return self.get(f'items/{uuid}')

	def create_item(self, **data) -> dict:
		return self.post_json('items', {k: v for k, v in data.items() if v is not None})

	def update_item(self, uuid: str, **data) -> dict:
		# Keep explicit None values (e.g. parent_uuid=None to clear parent) but strip
		# keys that were never provided (missing from kwargs means not updated).
		return self.put(f'items/{uuid}', data)

	def delete_item(self, uuid: str) -> dict:
		return self.delete(f'items/{uuid}')

	def upload_artefact(self, item_uuid: str, filepath: str, label: str,
	                    artefact_type: str = None, description: str = None,
	                    auto_analyse: bool = True,
	                    hints: dict = None,
	                    progress_cb=None) -> dict:
		"""Upload a file as a new artefact.

		Automatically uses chunked upload for files larger than CHUNKED_THRESHOLD.
		progress_cb(chunks_done, total_chunks) is called after each chunk (chunked only).

		hints: optional dict of analysis hints (e.g. {'dfi_clock_mhz': 100}).
		  Passed as a JSON string in the 'hints' form field.  The server forwards
		  these to queue_analyses_for_artefact() so they apply to every queued job.
		  Supported keys:
		    dfi_clock_mhz  (int)  — override DiscFerret sample frequency in MHz
		    platform       (str)  — platform hint (e.g. 'BBC Micro')
		    filesystem     (str)  — filesystem hint (e.g. 'adfs', 'fat12')
		"""
		if os.path.getsize(filepath) > CHUNKED_THRESHOLD:
			return self.upload_artefact_chunked(
				item_uuid, filepath, label,
				artefact_type=artefact_type,
				description=description,
				auto_analyse=auto_analyse,
				hints=hints,
				progress_cb=progress_cb,
			)
		fields = {'label': label}
		if artefact_type:
			fields['artefact_type'] = artefact_type
		if description:
			fields['description'] = description
		if not auto_analyse:
			fields['auto_analyse'] = 'false'
		if hints:
			fields['hints'] = json.dumps(hints)
		return self.post_file(f'items/{item_uuid}/artefacts/upload', filepath, fields)

	def _upload_chunk(self, upload_uuid: str, chunk_index: int, data: bytes) -> dict:
		"""POST a single raw binary chunk."""
		resp = self.session.post(
			self._url(f'uploads/chunked/{upload_uuid}/chunk/{chunk_index}'),
			data=data,
			headers={'Content-Type': 'application/octet-stream'},
		)
		return self._handle_response(resp)

	def upload_artefact_chunked(self, item_uuid: str, filepath: str, label: str,
	                             artefact_type: str = None, description: str = None,
	                             auto_analyse: bool = True,
	                             hints: dict = None,
	                             chunk_size: int = CHUNK_SIZE,
	                             progress_cb=None) -> dict:
		"""Upload a large file using the chunked upload protocol.

		Splits the file into chunk_size pieces, uploads each with per-chunk retry,
		then calls /complete to assemble and create the artefact.
		progress_cb(chunks_done, total_chunks) is called after each successful chunk.
		"""
		file_size = os.path.getsize(filepath)
		filename = os.path.basename(filepath)
		total_chunks = max(1, math.ceil(file_size / chunk_size))

		# Initialise upload session
		init_payload = {
			'filename': filename,
			'total_chunks': total_chunks,
			'total_size': file_size,
			'item_uuid': item_uuid,
			'label': label,
			'auto_analyse': auto_analyse,
		}
		if artefact_type:
			init_payload['artefact_type'] = artefact_type
		if description:
			init_payload['description'] = description
		if hints:
			init_payload['hints'] = hints

		init_result = self.post_json('uploads/chunked/init', init_payload)
		upload_uuid = init_result['upload_uuid']

		# Upload chunks with per-chunk retry (up to 3 attempts)
		with open(filepath, 'rb') as f:
			for chunk_index in range(total_chunks):
				data = f.read(chunk_size)
				last_exc = None
				for _attempt in range(3):
					try:
						self._upload_chunk(upload_uuid, chunk_index, data)
						break
					except Exception as exc:
						last_exc = exc
				else:
					raise ArcologyError(
						f'Chunk {chunk_index} failed after 3 attempts: {last_exc}'
					) from last_exc
				if progress_cb:
					progress_cb(chunk_index + 1, total_chunks)

		# Assemble and create artefact
		resp = self.session.post(self._url(f'uploads/chunked/{upload_uuid}/complete'))
		return self._handle_response(resp)

	def download_artefact(self, uuid: str, output_path: str):
		self.download(f'artefacts/{uuid}/download', output_path)

	def get_artefact(self, uuid: str) -> dict:
		return self.get(f'artefacts/{uuid}')

	def move_artefact(self, uuid: str, target_item_uuid: str) -> dict:
		return self.post_json(f'artefacts/{uuid}/move', {'target_item_uuid': target_item_uuid})

	def list_platforms(self) -> dict:
		return self.get('platforms')

	def list_categories(self) -> dict:
		return self.get('categories')

	def list_tags(self) -> dict:
		return self.get('tags')

	def get_analysis(self, uuid: str) -> dict:
		return self.get(f'analysis/{uuid}')

	def get_artefact_analyses_recursive(self, uuid: str, status: str = None) -> dict:
		params = {}
		if status:
			params['status'] = status
		return self.get(f'artefacts/{uuid}/analysis/recursive', params=params or None)

	def get_artefact_tree(self, uuid: str) -> dict:
		return self.get(f'artefacts/{uuid}/analysis/tree')

	def get_processing_tree(self, uuid: str) -> dict:
		return self.get(f'artefacts/{uuid}/processing-tree')

	def search_failures(self, **params) -> dict:
		return self.get('analysis/failures', params={k: v for k, v in params.items() if v is not None})

	# ---- Paginated helpers ----

	def list_items_all(self, **params) -> list[dict]:
		"""Fetch all items matching *params*, handling pagination automatically."""
		items = []
		params.setdefault('per_page', 100)
		params['page'] = 1
		while True:
			data = self.list_items(**params)
			items.extend(data.get('items', []))
			if params['page'] >= data.get('pages', 1):
				break
			params['page'] += 1
		return items

	def get_partition_files_all(self, partition_uuid: str, **params) -> list[dict]:
		"""Fetch all files in a partition, handling pagination automatically."""
		files = []
		params.setdefault('per_page', 500)
		params['page'] = 1
		while True:
			data = self.get(f'partitions/{partition_uuid}/files',
			                params={k: v for k, v in params.items() if v is not None})
			files.extend(data.get('files', []))
			if params['page'] >= data.get('pages', 1):
				break
			params['page'] += 1
		return files

	# ---- Lookup helpers ----

	def lookup_platform(self, name: str) -> int | None:
		"""Find a platform ID by name (case-insensitive). Returns None if not found."""
		if not name:
			return None
		for p in self.list_platforms().get('platforms', []):
			if p['name'].lower() == name.lower():
				return p['id']
		return None

	def lookup_category(self, name: str) -> int | None:
		"""Find a category ID by name (case-insensitive). Returns None if not found."""
		if not name:
			return None
		for c in self.list_categories().get('categories', []):
			if c['name'].lower() == name.lower():
				return c['id']
		return None

	def find_item(self, name: str, tag: str) -> dict | None:
		"""Find an existing item by exact *name* within items tagged *tag*."""
		items = self.list_items_all(q=name, tag=tag)
		for item in items:
			if item['name'] == name:
				return item
		return None

	def get_item_filenames(self, item_uuid: str) -> set[str]:
		"""Return the set of ``original_filename`` values on an item's artefacts."""
		item = self.get_item(item_uuid)
		return {
			art.get('original_filename', '')
			for art in item.get('artefacts', [])
		}

	# ---- Upload with retry ----

	def upload_artefact_retry(self, item_uuid: str, filepath: str, label: str,
	                          artefact_type: str = None, description: str = None,
	                          auto_analyse: bool = True,
	                          hints: dict = None,
	                          max_retries: int = 3) -> dict | None:
		"""Upload with exponential-backoff retry. Returns None on persistent failure."""
		for attempt in range(max_retries):
			try:
				return self.upload_artefact(
					item_uuid, filepath, label,
					artefact_type=artefact_type,
					description=description,
					auto_analyse=auto_analyse,
					hints=hints,
				)
			except (ArcologyError, requests.ConnectionError) as exc:
				log.warning('Upload attempt %d failed: %s', attempt + 1, exc)
				if attempt < max_retries - 1:
					wait = 2 ** (attempt + 1)
					log.info('  Retrying in %ds...', wait)
					time.sleep(wait)
		return None

	# ---- Hash database methods ----

	def list_hash_databases(self) -> list[dict]:
		return self.get('hash-databases')

	def get_hash_database(self, db_id: int) -> dict:
		return self.get(f'hash-databases/{db_id}')

	def create_hash_database(self, **data) -> dict:
		return self.post_json('hash-databases', {k: v for k, v in data.items() if v is not None})

	def create_hash_database_product(self, db_id: int, **data) -> dict:
		return self.post_json(f'hash-databases/{db_id}/products',
		                      {k: v for k, v in data.items() if v is not None})

	def add_product_files(self, db_id: int, product_id: int, files: list) -> dict:
		return self.post_json(f'hash-databases/{db_id}/products/{product_id}/files', files)


def compute_file_hashes(filepath: str) -> tuple[str, str]:
	"""Compute MD5 and SHA256 hashes for a file."""
	md5_hash = hashlib.md5()
	sha256_hash = hashlib.sha256()
	with open(filepath, 'rb') as f:
		for chunk in iter(lambda: f.read(8192), b''):
			md5_hash.update(chunk)
			sha256_hash.update(chunk)
	return md5_hash.hexdigest(), sha256_hash.hexdigest()

# vim: ts=4 sw=4 noet
