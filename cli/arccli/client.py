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
from .config import CONFIG_DIR

# Files larger than this threshold are uploaded in chunks
CHUNKED_THRESHOLD = 100 * 1024 * 1024   # 100 MB
CHUNK_SIZE        =  50 * 1024 * 1024   #  50 MB

# (connect, read) timeout applied to every request.  The read timeout only
# counts time spent waiting for the server to respond, not time spent
# streaming a request body, so large uploads are unaffected.  With async
# finalise the /complete and status requests all return promptly, so the read
# timeout no longer has to cover a multi-GB server-side assembly.
DEFAULT_TIMEOUT = (10, 300)

# Per-chunk retry, deliberately patient so a brief server outage (e.g. a
# redeploy mid-upload) is ridden out rather than failing the whole upload.
CHUNK_MAX_ATTEMPTS = 6
CHUNK_RETRY_MAX_BACKOFF = 30  # seconds

# Async-finalise polling: poll the status endpoint until the artefact is
# created or finalise fails.  Connection errors during polling are tolerated
# (the server may be briefly unavailable during a redeploy).
FINALIZE_POLL_MIN_INTERVAL = 2     # seconds
FINALIZE_POLL_MAX_INTERVAL = 10    # seconds
FINALIZE_POLL_TIMEOUT = 4 * 3600   # safety cap on total wait

# Sidecar file recording in-progress chunked uploads so they can be resumed
# (e.g. after a redeploy or a Ctrl-C) instead of re-uploading from scratch.
RESUME_STORE = CONFIG_DIR / 'resume.json'

log = logging.getLogger(__name__)


class ArcologyError(Exception):
	"""Base exception for Arcology API errors."""
	def __init__(self, message, status_code=None, response=None):
		super().__init__(message)
		self.status_code = status_code
		self.response = response


class ArcologyClient:
	"""HTTP client for the Arcology REST API."""

	def __init__(self, base_url: str, api_key: str, timeout=DEFAULT_TIMEOUT):
		self.base_url = base_url.rstrip('/')
		self.api_url = f"{self.base_url}/api"
		self.timeout = timeout
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
		resp = self.session.get(self._url(endpoint), params=params, timeout=self.timeout)
		return self._handle_response(resp)

	def post_json(self, endpoint: str, data: dict) -> dict:
		"""POST JSON data to API endpoint."""
		resp = self.session.post(self._url(endpoint), json=data, timeout=self.timeout)
		return self._handle_response(resp)

	def post_file(self, endpoint: str, filepath: str, fields: dict) -> dict:
		"""POST multipart file upload to API endpoint."""
		with open(filepath, 'rb') as f:
			files = {'file': (os.path.basename(filepath), f)}
			resp = self.session.post(self._url(endpoint), files=files, data=fields,
			                         timeout=self.timeout)
		return self._handle_response(resp)

	def put(self, endpoint: str, data: dict) -> dict:
		"""PUT JSON data to API endpoint."""
		resp = self.session.put(self._url(endpoint), json=data, timeout=self.timeout)
		return self._handle_response(resp)

	def delete(self, endpoint: str) -> dict:
		"""DELETE request to API endpoint."""
		resp = self.session.delete(self._url(endpoint), timeout=self.timeout)
		return self._handle_response(resp)

	def download(self, endpoint: str, output_path: str):
		"""Download a file from API endpoint to local path."""
		resp = self.session.get(self._url(endpoint), stream=True, timeout=self.timeout)
		if resp.status_code >= 400:
			self._handle_response(resp)
		try:
			with open(output_path, 'wb') as f:
				for chunk in resp.iter_content(chunk_size=8192):
					f.write(chunk)
		except BaseException:
			# Don't leave a partially-written file behind
			try:
				os.unlink(output_path)
			except OSError:
				pass
			raise

	# ---- Convenience methods ----

	def health(self) -> dict:
		"""Check server health (no auth required)."""
		resp = self.session.get(f"{self.api_url}/health", timeout=self.timeout)
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
	                    progress_cb=None, status_cb=None) -> dict:
		"""Upload a file as a new artefact.

		Automatically uses chunked upload for files larger than CHUNKED_THRESHOLD.
		progress_cb(chunks_done, total_chunks) is called after each chunk (chunked only).
		status_cb(state) is called while the server assembles a chunked upload.

		hints: optional dict of analysis hints (e.g. {'dfi_clock_mhz': 100}).
		  Passed as a JSON string in the 'hints' form field.  The server forwards
		  these to queue_analyses_for_artefact() so they apply to every queued job.
		  The accepted user-facing keys are the single source of truth in
		  arcology_shared.hints.UPLOAD_HINT_KEYS:
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
				status_cb=status_cb,
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
			timeout=self.timeout,
		)
		return self._handle_response(resp)

	def upload_artefact_chunked(self, item_uuid: str, filepath: str, label: str,
	                             artefact_type: str = None, description: str = None,
	                             auto_analyse: bool = True,
	                             hints: dict = None,
	                             chunk_size: int = CHUNK_SIZE,
	                             progress_cb=None, status_cb=None) -> dict:
		"""Upload a large file using the resumable chunked upload protocol.

		Splits the file into chunk_size pieces and uploads each with patient
		per-chunk retry, resuming a previously interrupted upload of the same file
		(skipping chunks the server already holds) rather than restarting.  The
		assembly is then driven asynchronously: /complete returns immediately and
		the client polls until the artefact is created, so a multi-GB assemble
		never trips the request timeout, and an assembly orphaned by a server
		redeploy is re-driven on the next poll.

		progress_cb(chunks_done, total_chunks) is called as chunks are uploaded.
		status_cb(state) is called with the finalise state ('assembling') while
		the server assembles.  Returns the created artefact dict.
		"""
		file_size = os.path.getsize(filepath)
		filename = os.path.basename(filepath)
		total_chunks = max(1, math.ceil(file_size / chunk_size))

		resume_key = self._resume_key(filepath, item_uuid, total_chunks)
		upload_uuid, received, artefact = self._resume_session(resume_key, total_chunks)
		if artefact is not None:
			# A previous run already finished finalise; nothing left to do.
			self._clear_resume(resume_key)
			return artefact

		if upload_uuid is None:
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
			upload_uuid = self.post_json('uploads/chunked/init', init_payload)['upload_uuid']
			received = set()
			self._save_resume(resume_key, upload_uuid)

		# Upload the chunks the server is still missing, seeking past those it
		# already has so a resume re-reads only what it must.
		with open(filepath, 'rb') as f:
			for chunk_index in range(total_chunks):
				if chunk_index in received:
					if progress_cb:
						progress_cb(chunk_index + 1, total_chunks)
					continue
				f.seek(chunk_index * chunk_size)
				self._upload_chunk_with_retry(upload_uuid, chunk_index, f.read(chunk_size))
				if progress_cb:
					progress_cb(chunk_index + 1, total_chunks)

		# Drive finalise asynchronously and wait for the artefact.
		artefact = self._complete_and_wait(upload_uuid, status_cb=status_cb)
		self._clear_resume(resume_key)
		return artefact

	def _upload_chunk_with_retry(self, upload_uuid: str, chunk_index: int,
	                              data: bytes) -> dict:
		"""Upload one chunk, retrying transient failures with backoff.

		A 4xx is permanent (bad session/index) and fails fast; network errors and
		5xx are retried, with enough patience to outlast a brief server outage.
		"""
		last_exc = None
		for attempt in range(CHUNK_MAX_ATTEMPTS):
			if attempt:
				time.sleep(min(2 ** attempt, CHUNK_RETRY_MAX_BACKOFF))
			try:
				return self._upload_chunk(upload_uuid, chunk_index, data)
			except ArcologyError as exc:
				if exc.status_code and 400 <= exc.status_code < 500:
					raise
				last_exc = exc
			except requests.RequestException as exc:
				last_exc = exc
		raise ArcologyError(
			f'Chunk {chunk_index} failed after {CHUNK_MAX_ATTEMPTS} attempts: {last_exc}'
		) from last_exc

	def _finalize_status(self, upload_uuid: str) -> dict:
		"""GET the async finalise status for a chunked upload session."""
		return self.get(f'uploads/chunked/{upload_uuid}/complete/status')

	def _complete_and_wait(self, upload_uuid: str, status_cb=None) -> dict:
		"""Request async finalise and poll until the artefact exists.

		Falls back gracefully to the synchronous response of an older server that
		does not understand the async flag (it returns 201 + artefact directly).
		"""
		resp = self.session.post(
			self._url(f'uploads/chunked/{upload_uuid}/complete'),
			json={'async': True}, timeout=self.timeout)
		if resp.status_code == 201:
			return self._handle_response(resp)  # old server: synchronous artefact
		if resp.status_code != 202:
			return self._handle_response(resp)  # raises on error
		return self._poll_finalize(upload_uuid, status_cb=status_cb)

	def _poll_finalize(self, upload_uuid: str, status_cb=None) -> dict:
		"""Poll /complete/status until done (returns artefact) or failed (raises).

		Transient connection errors are tolerated so a redeploy mid-finalise does
		not abort the upload; a 404 means the session expired before completing.
		"""
		interval = FINALIZE_POLL_MIN_INTERVAL
		deadline = time.time() + FINALIZE_POLL_TIMEOUT
		while time.time() < deadline:
			try:
				status = self._finalize_status(upload_uuid)
			except ArcologyError as exc:
				if exc.status_code == 404:
					raise ArcologyError(
						'Upload session expired before finalise completed', 404) from exc
				time.sleep(interval)
				interval = min(interval * 2, FINALIZE_POLL_MAX_INTERVAL)
				continue
			except requests.RequestException:
				time.sleep(interval)
				interval = min(interval * 2, FINALIZE_POLL_MAX_INTERVAL)
				continue
			state = status.get('state')
			if state == 'done':
				return status['artefact']
			if state == 'failed':
				raise ArcologyError(
					f"Server failed to finalise the upload: {status.get('error')}")
			if status_cb:
				status_cb(state)
			time.sleep(interval)
			interval = min(interval * 2, FINALIZE_POLL_MAX_INTERVAL)
		raise ArcologyError('Timed out waiting for the server to finalise the upload')

	# ---- Resumable-upload bookkeeping ----

	def _resume_key(self, filepath: str, item_uuid: str, total_chunks: int) -> str:
		"""Identity key for a resumable upload (server + target + file + chunking)."""
		st = os.stat(filepath)
		return '|'.join((self.base_url, item_uuid, os.path.abspath(filepath),
		                 str(st.st_size), str(int(st.st_mtime)), str(total_chunks)))

	def _resume_session(self, resume_key: str, total_chunks: int):
		"""Resolve a saved session to (upload_uuid, received_set, artefact|None).

		Returns (None, set(), None) to start fresh.  Inspects the server so a
		resume is only attempted when the session still exists and matches.
		"""
		saved = self._load_resume(resume_key)
		if not saved:
			return None, set(), None
		upload_uuid = saved.get('upload_uuid')
		try:
			fstatus = self._finalize_status(upload_uuid)
		except ArcologyError as exc:
			if exc.status_code == 404:
				self._clear_resume(resume_key)
				return None, set(), None
			raise
		except requests.RequestException:
			# Can't reach the server to verify; start fresh rather than guess.
			return None, set(), None
		state = fstatus.get('state')
		if state == 'done':
			return upload_uuid, set(), fstatus.get('artefact')
		if state == 'assembling':
			# All chunks are in; finalise is already running — just poll it.
			return upload_uuid, set(range(total_chunks)), None
		if state == 'failed':
			# A failed finalise is not re-claimable; restart cleanly.
			self._clear_resume(resume_key)
			return None, set(), None
		# pending: still uploading — fetch which chunks the server already holds.
		try:
			st = self.get(f'uploads/chunked/{upload_uuid}/status')
		except (ArcologyError, requests.RequestException):
			self._clear_resume(resume_key)
			return None, set(), None
		if st.get('total_chunks') != total_chunks:
			self._clear_resume(resume_key)
			return None, set(), None
		return upload_uuid, set(st.get('received_chunks') or []), None

	def _load_resume_all(self) -> dict:
		try:
			with open(RESUME_STORE) as f:
				return json.load(f)
		except (OSError, json.JSONDecodeError):
			return {}

	def _load_resume(self, key: str):
		return self._load_resume_all().get(key)

	def _write_resume_all(self, data: dict) -> None:
		# Best-effort: never fail an upload because resume state can't be written.
		try:
			CONFIG_DIR.mkdir(parents=True, exist_ok=True)
			tmp = RESUME_STORE.with_suffix('.tmp')
			with open(tmp, 'w') as f:
				json.dump(data, f)
			os.replace(tmp, RESUME_STORE)
		except OSError:
			pass

	def _save_resume(self, key: str, upload_uuid: str) -> None:
		data = self._load_resume_all()
		data[key] = {'upload_uuid': upload_uuid, 'ts': time.time()}
		self._write_resume_all(data)

	def _clear_resume(self, key: str) -> None:
		data = self._load_resume_all()
		if key in data:
			del data[key]
			self._write_resume_all(data)

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
	                          max_retries: int = 3,
	                          progress_cb=None) -> dict:
		"""Upload with exponential-backoff retry.

		Client errors (4xx) cannot succeed on retry and are re-raised
		immediately; connection and server errors are retried up to
		max_retries times.  Raises the last error on persistent failure —
		the same contract as upload_artefact(), so callers handle one
		error path instead of checking for None.

		progress_cb(chunks_done, total_chunks) is forwarded to the chunked
		uploader (files larger than CHUNKED_THRESHOLD only).
		"""
		last_exc = None
		for attempt in range(max_retries):
			try:
				return self.upload_artefact(
					item_uuid, filepath, label,
					artefact_type=artefact_type,
					description=description,
					auto_analyse=auto_analyse,
					hints=hints,
					progress_cb=progress_cb,
				)
			except (ArcologyError, requests.ConnectionError) as exc:
				if (isinstance(exc, ArcologyError) and exc.status_code
						and 400 <= exc.status_code < 500):
					raise
				last_exc = exc
				log.warning('Upload attempt %d failed: %s', attempt + 1, exc)
				if attempt < max_retries - 1:
					wait = 2 ** (attempt + 1)
					log.info('  Retrying in %ds...', wait)
					time.sleep(wait)
		raise last_exc

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

	def import_hash_database_products(self, db_id: int, products: list,
	                                  link: bool = True) -> dict:
		"""Batch-import products (each with a ``files`` list) in one request.

		Raises ArcologyError(status_code=404) against servers that predate the
		batch import endpoint, so callers can fall back to the per-product path.
		"""
		return self.post_json(
			f'hash-databases/{db_id}/import',
			{'products': products, 'link': link},
		)

	def queue_hash_database_link(self, db_id: int) -> dict:
		"""Queue a worker-side relink job for a hash database."""
		return self.post_json(f'hash-databases/{db_id}/link', {})

	def hash_lookup(self, md5: str = None, sha1: str = None) -> dict:
		"""Look up a file by hash: returns any matching KnownFile and every
		extracted-file occurrence across the (visible) collection."""
		params = {}
		if md5:
			params['md5'] = md5
		if sha1:
			params['sha1'] = sha1
		return self.get('hash-lookup', params=params)

	def download_extracted_file_bytes(self, uuid: str) -> bytes:
		"""Return the raw bytes of a single extracted file (e.g. a RISC OS
		!Run Obey file).  Restricted artefacts return an error."""
		resp = self.session.get(self._url(f'files/{uuid}/download'),
		                        timeout=self.timeout)
		if resp.status_code >= 400:
			self._handle_response(resp)
		return resp.content


def verify_artefact_hashes(filepath: str, result: dict) -> bool | None:
	"""Compare a just-uploaded file's local hashes with the server's record.

	Returns True when every hash the server returned matches, False on any
	mismatch (likely transfer corruption), and None when the server returned
	no hashes to compare against — callers should report that distinctly
	rather than treating it as verified.
	"""
	server_md5 = result.get('md5')
	server_sha256 = result.get('sha256')
	if not server_md5 and not server_sha256:
		return None
	local_md5, local_sha256 = compute_file_hashes(filepath)
	if server_md5 and server_md5 != local_md5:
		return False
	if server_sha256 and server_sha256 != local_sha256:
		return False
	return True


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
