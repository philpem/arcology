"""
HTTP client for the Arcology REST API.

Modelled on worker/arcworker/api.py but adapted for CLI use:
- Uses requests.Session for connection reuse
- Raises exceptions on errors instead of returning None
- Supports multipart file upload and streaming download
"""

import hashlib
import sys

import requests


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
			files = {'file': (filepath.split('/')[-1], f)}
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
	                    auto_analyse: bool = True) -> dict:
		fields = {'label': label}
		if artefact_type:
			fields['artefact_type'] = artefact_type
		if description:
			fields['description'] = description
		if not auto_analyse:
			fields['auto_analyse'] = 'false'
		return self.post_file(f'items/{item_uuid}/artefacts/upload', filepath, fields)

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
