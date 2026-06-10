"""
Storage backend abstraction for Arcology.

Provides a unified interface for file storage with two implementations:
- LocalStorage: Files on local filesystem (default, zero-cost self-hosting)
- S3Storage: S3-compatible object storage (enables distributed workers)

Both the web application and analysis worker import from this module.
The backend is selected via the STORAGE_BACKEND configuration variable.
"""

import abc
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

# Ensure SVG is always mapped correctly — some Linux systems omit it.
mimetypes.add_type('image/svg+xml', '.svg')


def _mime_for_key(key: str) -> str:
    """Return the MIME type for a storage key based on its file extension."""
    mime, _ = mimetypes.guess_type(key)
    return mime or 'application/octet-stream'


# Characters that must never appear raw in a Content-Disposition value:
# control chars (incl. CR/LF), double-quote and backslash.  A user-supplied
# original filename can legitimately contain '"' and — because the upload-time
# sanitiser deliberately preserves most characters — even CR/LF.  Whatever
# bytes land in ResponseContentDisposition are reproduced verbatim by S3 in the
# response header, so an unsanitised filename is a header-injection vector
# (CWE-113).  Neutralise them here.
_CD_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\]')


def _content_disposition_attachment(filename: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` value for *filename*.

    Produces an ASCII ``filename="..."`` with all unsafe characters replaced,
    plus an RFC 5987 ``filename*=UTF-8''...`` parameter (percent-encoded, so it
    never contains raw control characters) that carries the original name with
    full fidelity for browsers that support it.  The result is guaranteed to
    contain no raw CR/LF, NUL, quote or backslash, so it cannot inject or split
    HTTP response headers when echoed back by the storage backend.
    """
    ascii_name = _CD_UNSAFE.sub('_', filename)
    ascii_name = ascii_name.encode('ascii', 'ignore').decode('ascii').strip()
    if not ascii_name:
        ascii_name = 'download'
    encoded = quote(filename, safe='')
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"

# botocore exception base classes — imported here so the module loads even
# when boto3 is absent (LocalStorage users never import these).
try:
    from botocore.exceptions import BotoCoreError
    from botocore.exceptions import ClientError as BotoClientError
except ImportError:
    BotoCoreError = BotoClientError = Exception  # type: ignore[misc,assignment]


class _GarageHeaderParsingFilter(logging.Filter):
    """Suppress urllib3 HeaderParsingError warnings from Garage and similar
    S3-compatible backends whose HTTP responses trigger Python's email parser
    defect detection.  The operations succeed correctly; the warning is purely
    cosmetic but generates a full traceback per uploaded object."""
    def filter(self, record: logging.LogRecord) -> bool:
        return 'Failed to parse headers' not in record.getMessage()

log = logging.getLogger(__name__)


class StorageBackend(abc.ABC):
    """Abstract interface for file storage."""

    @abc.abstractmethod
    def put(self, key: str, source_path: Path) -> None:
        """Upload a local file to storage at the given key."""

    @abc.abstractmethod
    def get(self, key: str, dest_path: Path) -> None:
        """Download a storage object to a local file path."""

    @abc.abstractmethod
    def open_read(self, key: str) -> BinaryIO:
        """Return a file-like object for reading the stored object."""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Delete a single object by key.  No error if it doesn't exist."""

    @abc.abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete all objects under a key prefix.  Returns count deleted."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """Check if an object exists."""

    @abc.abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """List all object keys under a prefix."""

    @abc.abstractmethod
    def presigned_url(self, key: str, expires: int = 3600, filename: str | None = None) -> str | None:
        """Return a pre-signed download URL, or None if not supported."""

    @abc.abstractmethod
    def put_tree(self, prefix: str, local_dir: Path) -> int:
        """Upload an entire directory tree under a prefix.  Returns file count."""

    @abc.abstractmethod
    def get_tree(self, prefix: str, dest_dir: Path) -> int:
        """Download all objects under a prefix into a local directory.  Returns file count."""

    def storage_key(self, storage_directory: str, storage_path: str) -> str:
        """Build a storage key from a StorageDirectory value and a storage_path.

        E.g. storage_key('uploads', 'abc123.img') -> 'uploads/abc123.img'
        """
        return f"{storage_directory}/{storage_path}"


class LocalStorage(StorageBackend):
    """Local filesystem storage backend.

    Keys are mapped to filesystem paths:
      uploads/<name>  ->  {uploads_dir}/<name>
      outputs/<name>  ->  {outputs_dir}/<name>
    """

    def __init__(self, uploads_dir: Path, outputs_dir: Path):
        self.uploads_dir = Path(uploads_dir)
        self.outputs_dir = Path(outputs_dir)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve a storage key to a local filesystem path.

        Raises ValueError if the key would escape the storage directory
        (e.g. via ``..`` traversal).
        """
        if key.startswith('uploads/'):
            root = self.uploads_dir
            rel = key[len('uploads/'):]
        elif key.startswith('outputs/'):
            root = self.outputs_dir
            rel = key[len('outputs/'):]
        else:
            raise ValueError(f"Invalid storage key (must start with 'uploads/' or 'outputs/'): {key!r}")
        resolved = (root / rel).resolve()
        if not str(resolved).startswith(str(root.resolve()) + os.sep) and resolved != root.resolve():
            raise ValueError(f"Path traversal detected in storage key: {key!r}")
        return resolved

    def put(self, key: str, source_path: Path) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = Path(source_path)
        # No-op if source and dest are the same file
        if source.resolve() == dest.resolve():
            return
        shutil.copy2(source, dest)

    def get(self, key: str, dest_path: Path) -> None:
        source = self._resolve(key)
        dest = Path(dest_path)
        if source.resolve() == dest.resolve():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)

    def open_read(self, key: str) -> BinaryIO:
        return open(self._resolve(key), 'rb')

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def delete_prefix(self, prefix: str) -> int:
        path = self._resolve(prefix)
        if not path.exists():
            return 0
        if path.is_dir():
            count = sum(1 for _ in path.rglob('*') if _.is_file())
            shutil.rmtree(path)
            return count
        # Single file
        path.unlink()
        return 1

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        base = self._resolve(prefix)
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        # Walk directory and return keys
        results = []
        # Determine which root we're under to reconstruct keys
        if prefix.startswith('uploads/'):
            root_dir = self.uploads_dir.resolve()
            root_prefix = 'uploads/'
        else:
            root_dir = self.outputs_dir.resolve()
            root_prefix = 'outputs/'
        for path in base.rglob('*'):
            if path.is_file():
                rel = path.resolve().relative_to(root_dir)
                results.append(root_prefix + str(rel))
        return results

    def presigned_url(self, key: str, expires: int = 3600, filename: str | None = None) -> str | None:
        # Local storage doesn't support pre-signed URLs
        return None

    def put_tree(self, prefix: str, local_dir: Path) -> int:
        dest = self._resolve(prefix)
        local_dir = Path(local_dir)
        if local_dir.resolve() == dest.resolve():
            return sum(1 for _ in local_dir.rglob('*') if _.is_file())
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(local_dir, dest)
        return sum(1 for _ in dest.rglob('*') if _.is_file())

    def get_tree(self, prefix: str, dest_dir: Path) -> int:
        source = self._resolve(prefix)
        dest_dir = Path(dest_dir)
        if not source.exists():
            return 0
        if source.resolve() == dest_dir.resolve():
            return sum(1 for _ in source.rglob('*') if _.is_file())
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest_dir, dirs_exist_ok=True)
        return sum(1 for _ in dest_dir.rglob('*') if _.is_file())

    def local_path(self, key: str) -> Path:
        """Return the local filesystem path for a key (LocalStorage only)."""
        return self._resolve(key)


class S3Storage(StorageBackend):
    """S3-compatible object storage backend.

    Works with any S3-compatible service: AWS S3, Garage, SeaweedFS, etc.
    Keys map directly to S3 object keys within a single bucket.
    """

    def __init__(self, endpoint_url: str, bucket: str,
                 access_key: str, secret_key: str, region: str = 'us-east-1',
                 public_url: str | None = None):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        # request_checksum_calculation / response_checksum_validation:
        # botocore ≥1.35 added default SHA256 checksums on all S3 requests.
        # Many S3-compatible backends (Garage, MinIO older versions, etc.)
        # don't handle these extra headers correctly and respond with
        # FlexibleChecksumError.  Setting 'when_required' / 'when_supported'
        # restores the pre-1.35 behaviour: only send/validate checksums when
        # the operation explicitly requires them.
        # Older botocore (<1.35) doesn't recognise these keys, so fall back.
        try:
            _cfg = BotoConfig(
                signature_version='s3v4',
                request_checksum_calculation='when_required',
                response_checksum_validation='when_supported',
            )
        except TypeError:
            _cfg = BotoConfig(signature_version='s3v4')
        self._client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=_cfg,
        )
        # Presigned URLs must be signed against the hostname the browser
        # will use.  When S3 runs inside Docker (e.g. Garage at
        # http://garage:3900), that internal hostname is unreachable from
        # the browser.  A separate client configured with the public URL
        # ensures the signature matches the host the browser sends.
        if public_url and public_url != endpoint_url:
            self._public_client = boto3.client(
                's3',
                endpoint_url=public_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=_cfg,
            )
        else:
            self._public_client = self._client
        log.info(f"S3 storage: endpoint={endpoint_url} bucket={bucket}")

        # Garage (and some other S3-compatible backends) produce HTTP responses
        # that trigger urllib3's HeaderParsingError warning on every PUT.  The
        # uploads succeed; the warning is cosmetic but emits a full traceback
        # per object, drowning real errors.  Install a one-time log filter.
        _urllib3_pool_log = logging.getLogger('urllib3.connectionpool')
        if not any(isinstance(f, _GarageHeaderParsingFilter)
                   for f in _urllib3_pool_log.filters):
            _urllib3_pool_log.addFilter(_GarageHeaderParsingFilter())

    def put(self, key: str, source_path: Path) -> None:
        try:
            self._client.upload_file(
                str(source_path), self.bucket, key,
                ExtraArgs={'ContentType': _mime_for_key(key)},
            )
        except (BotoCoreError, BotoClientError) as exc:
            raise OSError(f"S3 upload failed for '{key}': {exc}") from exc

    def get(self, key: str, dest_path: Path) -> None:
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self.bucket, key, str(dest))
        except BotoClientError as exc:
            code = exc.response.get('Error', {}).get('Code', '')
            if code in ('404', 'NoSuchKey'):
                raise FileNotFoundError(f"S3 object not found: '{key}'") from exc
            raise OSError(f"S3 download failed for '{key}': {exc}") from exc
        except BotoCoreError as exc:
            raise OSError(f"S3 download failed for '{key}': {exc}") from exc

    def open_read(self, key: str) -> BinaryIO:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            # Wrap the streaming body in a SpooledTemporaryFile so callers
            # get a seekable file-like object.
            spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
            for chunk in resp['Body'].iter_chunks(8192):
                spool.write(chunk)
            spool.seek(0)
            return spool
        except BotoClientError as exc:
            code = exc.response.get('Error', {}).get('Code', '')
            if code in ('404', 'NoSuchKey'):
                raise FileNotFoundError(f"S3 object not found: '{key}'") from exc
            raise OSError(f"S3 read failed for '{key}': {exc}") from exc
        except BotoCoreError as exc:
            raise OSError(f"S3 read failed for '{key}': {exc}") from exc

    def delete(self, key: str) -> None:
        # S3 delete is idempotent — no error if key doesn't exist
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, BotoClientError) as exc:
            raise OSError(f"S3 delete failed for '{key}': {exc}") from exc

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_prefix(prefix)
        if not keys:
            return 0
        # Delete in batches of 1000 (S3 limit)
        deleted = 0
        try:
            for i in range(0, len(keys), 1000):
                batch = keys[i:i + 1000]
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={'Objects': [{'Key': k} for k in batch], 'Quiet': True}
                )
                deleted += len(batch)
        except (BotoCoreError, BotoClientError) as exc:
            raise OSError(f"S3 delete_prefix failed for '{prefix}': {exc}") from exc
        return deleted

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] in ('404', 'NoSuchKey'):
                return False
            raise

    def list_prefix(self, prefix: str) -> list[str]:
        keys = []
        paginator = self._client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys.append(obj['Key'])
        return keys

    def presigned_url(self, key: str, expires: int = 3600, filename: str | None = None) -> str | None:
        params = {
            'Bucket': self.bucket,
            'Key': key,
            'ResponseContentType': _mime_for_key(key),
        }
        if filename:
            params['ResponseContentDisposition'] = _content_disposition_attachment(filename)
        return self._public_client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=expires,
        )

    def put_tree(self, prefix: str, local_dir: Path) -> int:
        local_dir = Path(local_dir)
        prefix = prefix.rstrip('/') + '/'
        count = 0
        try:
            for path in local_dir.rglob('*'):
                if path.is_file():
                    rel = path.relative_to(local_dir)
                    key = prefix + str(rel)
                    self._client.upload_file(
                        str(path), self.bucket, key,
                        ExtraArgs={'ContentType': _mime_for_key(key)},
                    )
                    count += 1
        except (BotoCoreError, BotoClientError) as exc:
            raise OSError(f"S3 put_tree failed for prefix '{prefix}': {exc}") from exc
        return count

    def get_tree(self, prefix: str, dest_dir: Path) -> int:
        dest_dir = Path(dest_dir)
        prefix = prefix.rstrip('/') + '/'
        keys = self.list_prefix(prefix)
        count = 0
        try:
            for key in keys:
                rel = key[len(prefix):]
                if not rel:
                    continue
                dest = dest_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._client.download_file(self.bucket, key, str(dest))
                count += 1
        except (BotoCoreError, BotoClientError) as exc:
            raise OSError(f"S3 get_tree failed for prefix '{prefix}': {exc}") from exc
        return count


def create_storage(config: dict) -> StorageBackend:
    """Create a storage backend from configuration.

    Args:
        config: Dict with at least STORAGE_BACKEND key.
            For 'local': UPLOAD_FOLDER and OUTPUT_FOLDER (or defaults).
            For 's3': S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY,
                       and optionally S3_REGION.

    Returns:
        Configured StorageBackend instance.
    """
    backend = config.get('STORAGE_BACKEND', 'local').lower()

    if backend == 's3':
        endpoint = config.get('S3_ENDPOINT_URL')
        bucket = config.get('S3_BUCKET', 'arcology')
        access_key = config.get('S3_ACCESS_KEY')
        secret_key = config.get('S3_SECRET_KEY')
        region = config.get('S3_REGION', 'us-east-1')
        public_url = config.get('S3_PUBLIC_URL')

        if not all([endpoint, access_key, secret_key]):
            raise RuntimeError(
                "S3 storage requires S3_ENDPOINT_URL, S3_ACCESS_KEY, and S3_SECRET_KEY"
            )

        return S3Storage(
            endpoint_url=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            public_url=public_url,
        )

    if backend != 'local':
        raise ValueError(
            f"Invalid STORAGE_BACKEND: {backend!r}. Must be 'local' or 's3'."
        )

    uploads = Path(config.get('UPLOAD_FOLDER', 'uploads'))
    outputs = Path(config.get('OUTPUT_FOLDER', 'outputs'))
    return LocalStorage(uploads_dir=uploads, outputs_dir=outputs)

# vim: ts=4 sw=4 et
