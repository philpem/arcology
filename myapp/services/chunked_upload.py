"""Arcology - Shared chunked-upload storage and assembly.

Both the API blueprint (API-key clients such as the ``arco`` CLI) and the web
blueprint (cookie-authenticated browser sessions) drive the same chunked-upload
protocol:

    init    -> create a session directory + meta.json
    chunk   -> write each numbered chunk to the session directory
    status  -> report which chunks have arrived (resume support)
    complete-> assemble the chunks into the storage backend + hash them

This module owns the filesystem mechanics so the two blueprints share one
implementation.  Chunks are stored locally under
``<instance_path>/.chunks/<upload_uuid>/`` regardless of the active storage
backend (local or S3); on completion they are assembled into a tempfile and
pushed via ``storage.put()``, mirroring save_uploaded_file().

Callers translate the exceptions raised here into their own error formats
(JSON for the API, flash/redirect for the web UI).
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from flask import current_app
from ..utils.config import int_config
from .artefact_storage import get_storage_extension

UPLOAD_UUID_RE = re.compile(r'^[0-9a-f]{32}$')

CHUNK_STALE_SECONDS = 86400  # purge abandoned chunk dirs after 24 h

# Hard cap on the number of chunks a single session may declare.  This bounds
# the work/memory of complete (which builds a per-chunk path list) so a caller
# cannot trigger memory exhaustion by declaring a huge total_chunks.  At the
# default 50 MB chunk size this still allows multi-terabyte uploads.
MAX_TOTAL_CHUNKS = 100_000

# Default ceiling on the assembled artefact size for chunked uploads (the only
# path that can exceed the single-request MAX_CONTENT_LENGTH).  Override with the
# MAX_UPLOAD_SIZE config key / env var.
DEFAULT_MAX_UPLOAD_SIZE = 16 * 1024 * 1024 * 1024  # 16 GiB


class ChunkSessionCorrupt(Exception):
    """Raised when a session's meta.json is missing or unreadable."""


class StorageUnavailable(Exception):
    """Raised when the storage backend rejects the assembled upload."""


class UploadTooLarge(Exception):
    """Raised when the assembled bytes exceed the configured size cap."""

    def __init__(self, max_size: int):
        self.max_size = max_size
        super().__init__(f'Upload exceeds the maximum size of {max_size} bytes')


@dataclass
class AssembledUpload:
    """Result of assembling a chunked upload into the storage backend."""

    storage_name: str
    file_size: int
    md5: str
    sha256: str


def max_upload_size() -> int:
    """Return the configured maximum assembled chunked-upload size, in bytes."""
    return int_config('MAX_UPLOAD_SIZE', DEFAULT_MAX_UPLOAD_SIZE)


def chunks_base() -> str:
    """Return (and create) the base directory for in-progress chunk uploads."""
    path = os.path.join(current_app.instance_path, '.chunks')
    os.makedirs(path, exist_ok=True)
    return path


def chunk_dir(upload_uuid: str) -> str:
    return os.path.join(chunks_base(), upload_uuid)


def purge_stale_chunks() -> None:
    """Remove chunk directories that have not been touched in > 24 h."""
    base = chunks_base()
    cutoff = datetime.now(timezone.utc).timestamp() - CHUNK_STALE_SECONDS
    try:
        for name in os.listdir(base):
            if not UPLOAD_UUID_RE.match(name):
                continue
            path = os.path.join(base, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def init_chunk_session(meta: dict) -> str:
    """Create a new session directory, write meta.json, return its hex uuid."""
    upload_uuid = uuid.uuid4().hex
    cdir = chunk_dir(upload_uuid)
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, 'meta.json'), 'w') as f:
        json.dump(meta, f)
    return upload_uuid


def read_meta(upload_uuid: str) -> dict | None:
    """Load a session's meta.json.

    Returns None when the session directory does not exist; raises
    ChunkSessionCorrupt when the directory exists but meta.json is unreadable.
    """
    cdir = chunk_dir(upload_uuid)
    if not os.path.isdir(cdir):
        return None
    try:
        with open(os.path.join(cdir, 'meta.json')) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ChunkSessionCorrupt(str(exc)) from exc


def write_chunk(upload_uuid: str, chunk_index: int, data: bytes) -> None:
    """Write a single numbered chunk into the session directory.

    The caller is responsible for validating the uuid, session existence,
    ownership, and chunk_index range before calling this.
    """
    chunk_path = os.path.join(chunk_dir(upload_uuid), f'{chunk_index:06d}')
    with open(chunk_path, 'wb') as f:
        f.write(data)


def received_chunks(upload_uuid: str) -> list[int]:
    """Return the sorted indices of chunks that have arrived for a session."""
    cdir = chunk_dir(upload_uuid)
    return sorted(
        int(name) for name in os.listdir(cdir)
        if name != 'meta.json' and name.isdigit()
    )


def missing_chunks(upload_uuid: str, total_chunks: int) -> list[int]:
    """Return the indices in [0, total_chunks) that have not yet arrived."""
    cdir = chunk_dir(upload_uuid)
    return [
        i for i in range(total_chunks)
        if not os.path.exists(os.path.join(cdir, f'{i:06d}'))
    ]


def assemble_to_storage(upload_uuid: str, original_filename: str, *,
                        total_chunks: int, max_size: int) -> AssembledUpload:
    """Assemble all chunks into the storage backend and hash them.

    Concatenates chunks 0..total_chunks-1 into a tempfile (computing MD5 and
    SHA-256 inline), enforces *max_size* authoritatively, pushes the assembled
    file to the storage backend, then removes the session directory and purges
    stale sessions.

    Raises UploadTooLarge if the real size exceeds *max_size* (the session
    directory is removed first), and StorageUnavailable if the backend rejects
    the upload.
    """
    cdir = chunk_dir(upload_uuid)
    chunk_files = [os.path.join(cdir, f'{i:06d}') for i in range(total_chunks)]

    ext = get_storage_extension(original_filename)
    storage_name = f'{uuid.uuid4().hex}{ext}'

    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()
    file_size = 0
    oversize = False

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            for chunk_file in chunk_files:
                with open(chunk_file, 'rb') as cf:
                    while True:
                        buf = cf.read(65536)
                        if not buf:
                            break
                        tmp.write(buf)
                        md5_hash.update(buf)
                        sha256_hash.update(buf)
                        file_size += len(buf)
                        # Authoritative size cap (the declared total_size is only
                        # advisory).  Stop reading rather than spool an unbounded
                        # file to disk.
                        if file_size > max_size:
                            oversize = True
                            break
                if oversize:
                    break

        if oversize:
            shutil.rmtree(cdir, ignore_errors=True)
            raise UploadTooLarge(max_size)

        # Push assembled file to storage backend
        storage_key = current_app.storage.storage_key('uploads', storage_name)
        try:
            current_app.storage.put(storage_key, tmp_path)
        except OSError as exc:
            raise StorageUnavailable(str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Clean up chunk directory and purge any stale sessions
    shutil.rmtree(cdir, ignore_errors=True)
    purge_stale_chunks()

    return AssembledUpload(
        storage_name=storage_name,
        file_size=file_size,
        md5=md5_hash.hexdigest(),
        sha256=sha256_hash.hexdigest(),
    )

# vim: ts=4 sw=4 et
