"""Arcology - Shared chunked-upload storage and assembly.

Both the API blueprint (API-key clients such as the ``arco`` CLI) and the web
blueprint (cookie-authenticated browser sessions) drive the same chunked-upload
protocol:

    init    -> create a session directory + meta.json
    chunk   -> write each numbered chunk to the session directory
    status  -> report which chunks have arrived (resume support)
    complete-> assemble the chunks into the storage backend + hash them

This module owns the filesystem mechanics so the two blueprints share one
implementation.  Chunks are staged on the local filesystem under
``<CHUNK_DIR>/<upload_uuid>/`` regardless of the active storage backend (local
or S3); on completion they are assembled into a tempfile and pushed via
``storage.put()``, mirroring save_uploaded_file().

``CHUNK_DIR`` (config key / env var) selects the staging location and
**must point at a persisted volume in any containerised deployment**: an
in-progress upload can be tens of gigabytes, and the default
``<instance_path>/.chunks`` sits on the container's ephemeral writable layer,
so a container recreate (deploy) would both discard resumable sessions and
risk staging large uploads on a small rootfs (or tmpfs/RAM).  A relative
``CHUNK_DIR`` is resolved against the Flask instance path.

Callers translate the exceptions raised here into their own error formats
(JSON for the API, flash/redirect for the web UI).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from flask import current_app
from ..utils.config import int_config
from .artefact_storage import get_storage_extension

UPLOAD_UUID_RE = re.compile(r'^[0-9a-f]{32}$')

CHUNK_STALE_SECONDS = 86400  # purge abandoned chunk dirs after 24 h

# Finalise tuning defaults (overridable via config keys / env vars of the same
# name).  See doc/CHUNKED_UPLOADS.md for the asynchronous-finalise design.
DEFAULT_FINALIZE_CONCURRENCY = 2      # max concurrent finalises per web process
DEFAULT_FINALIZE_HEARTBEAT_SECONDS = 15
DEFAULT_FINALIZE_STALE_SECONDS = 120  # age of heartbeat_at before re-drive
DEFAULT_FINALIZE_RESULT_TTL_SECONDS = 3600  # retain done/failed sessions this long

# Finalise lifecycle states stored in meta.json under 'finalize_state'.  A
# session with no finalize_state key is implicitly PENDING (not yet finalised,
# and therefore claimable).
FINALIZE_ASSEMBLING = 'assembling'
FINALIZE_DONE = 'done'
FINALIZE_FAILED = 'failed'

# Per-session advisory lock file used to make claim_finalize() atomic across
# gunicorn worker *processes* on the same host (a threading.Lock only serialises
# within one process).  Held only for the brief claim read-modify-write.
_LOCK_NAME = '.finalize.lock'

# Serialises read-modify-write of meta.json within a process so the atomic
# claim and the heartbeat updates do not interleave.  Cross-process atomicity of
# the claim additionally rests on the _LOCK_NAME flock (see _claim_flock).
_finalize_lock = threading.Lock()
_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None

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


def finalize_concurrency() -> int:
    """Max concurrent finalise jobs per web process."""
    return max(1, int_config('FINALIZE_CONCURRENCY', DEFAULT_FINALIZE_CONCURRENCY))


def finalize_heartbeat_seconds() -> int:
    """Cadence at which a running finalise refreshes its heartbeat."""
    return max(1, int_config('FINALIZE_HEARTBEAT_SECONDS',
                             DEFAULT_FINALIZE_HEARTBEAT_SECONDS))


def finalize_stale_seconds() -> int:
    """Age of heartbeat_at past which an 'assembling' session is re-drivable."""
    return max(1, int_config('FINALIZE_STALE_SECONDS',
                             DEFAULT_FINALIZE_STALE_SECONDS))


def finalize_result_ttl_seconds() -> int:
    """How long a done/failed session is retained for status polling."""
    return max(1, int_config('FINALIZE_RESULT_TTL_SECONDS',
                             DEFAULT_FINALIZE_RESULT_TTL_SECONDS))


def chunks_base() -> str:
    """Return (and create) the base directory for in-progress chunk uploads.

    Honours the ``CHUNK_DIR`` config key / env var (resolved against the Flask
    instance path when relative); defaults to ``<instance_path>/.chunks``.
    See the module docstring for why this must be a persisted volume in
    containerised deployments.
    """
    configured = current_app.config.get('CHUNK_DIR')
    if configured:
        path = configured if os.path.isabs(configured) \
            else os.path.join(current_app.instance_path, configured)
    else:
        path = os.path.join(current_app.instance_path, '.chunks')
    os.makedirs(path, exist_ok=True)
    return path


def chunk_dir(upload_uuid: str) -> str:
    return os.path.join(chunks_base(), upload_uuid)


def purge_stale_chunks() -> None:
    """Reap finished and abandoned chunk sessions.

    Removes a session directory when either:
      - it holds a done/failed finalise result older than the result TTL
        (the retained meta.json has served its purpose for status polling), or
      - it has not been touched in > CHUNK_STALE_SECONDS (abandoned upload).
    """
    base = chunks_base()
    now = time.time()
    mtime_cutoff = now - CHUNK_STALE_SECONDS
    result_ttl = finalize_result_ttl_seconds()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not UPLOAD_UUID_RE.match(name):
            continue
        path = os.path.join(base, name)
        try:
            # Stat first; only parse meta.json when the directory is recent
            # enough that it might be a finished session still inside its TTL.
            # A dir untouched for > CHUNK_STALE_SECONDS is abandoned regardless
            # of state (an active finalise's heartbeat keeps its mtime fresh).
            if os.stat(path).st_mtime < mtime_cutoff:
                shutil.rmtree(path, ignore_errors=True)
                continue
            meta = _read_meta_dir(path)
            if meta and meta.get('finalize_state') in (FINALIZE_DONE, FINALIZE_FAILED):
                if now - meta.get('finalized_at', 0) > result_ttl:
                    shutil.rmtree(path, ignore_errors=True)
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

    Returns None when the upload_uuid is malformed or the session directory does
    not exist; raises ChunkSessionCorrupt when the directory exists but meta.json
    is unreadable.  Validating the uuid format here keeps the (path-building)
    callers safe from traversal without each repeating the check.
    """
    if not UPLOAD_UUID_RE.match(upload_uuid):
        return None
    cdir = chunk_dir(upload_uuid)
    if not os.path.isdir(cdir):
        return None
    try:
        with open(os.path.join(cdir, 'meta.json')) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ChunkSessionCorrupt(str(exc)) from exc


def _read_meta_dir(cdir: str) -> dict | None:
    """Load meta.json from a resolved session directory (no app context needed).

    Returns None if the directory or meta.json is missing/unreadable.  Used by
    the finalise machinery and the heartbeat thread, which run off the request
    context and operate on an already-resolved absolute path.
    """
    try:
        with open(os.path.join(cdir, 'meta.json')) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta_dir(cdir: str, meta: dict) -> None:
    """Atomically replace meta.json in a resolved session directory.

    Writes to a temp file in the same directory and os.replace()s it into place
    so a concurrent reader (or a crash mid-write) never observes a partial file.
    """
    fd, tmp = tempfile.mkstemp(dir=cdir, prefix='.meta-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(cdir, 'meta.json'))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
                        total_chunks: int, max_size: int,
                        cleanup: bool = True) -> AssembledUpload:
    """Assemble all chunks into the storage backend and hash them.

    Concatenates chunks 0..total_chunks-1 into a tempfile (computing MD5 and
    SHA-256 inline), enforces *max_size* authoritatively, pushes the assembled
    file to the storage backend, then (when *cleanup*) removes the session
    directory and purges stale sessions.

    Pass ``cleanup=False`` from the asynchronous finalise runner, which keeps
    the session directory so meta.json can record the outcome for status
    polling and manages chunk-file removal itself.

    Raises UploadTooLarge if the real size exceeds *max_size* (the session
    directory is removed first only when *cleanup*), and StorageUnavailable if
    the backend rejects the upload.
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
            if cleanup:
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

    # Clean up chunk directory and purge any stale sessions.  The async finalise
    # runner passes cleanup=False and owns the session lifecycle itself.
    if cleanup:
        shutil.rmtree(cdir, ignore_errors=True)
        purge_stale_chunks()

    return AssembledUpload(
        storage_name=storage_name,
        file_size=file_size,
        md5=md5_hash.hexdigest(),
        sha256=sha256_hash.hexdigest(),
    )


# =============================================================================
# Asynchronous finalise
#
# Finalise (assemble + hash + store + ingest) is multi-minute work for a large
# upload and must not run on the HTTP request thread (it would blow the gunicorn
# timeout and surface as a 504).  When a client opts in, /complete claims the
# session and submits the work to a small per-process thread pool, then returns
# immediately; the client polls /complete/status for the result.
#
# Job state lives in the session's meta.json (on the persisted CHUNK_DIR
# volume), so it survives a web-container restart and a finalise orphaned by a
# redeploy is re-driven when the client next polls.  See doc/CHUNKED_UPLOADS.md.
# =============================================================================


def _delete_chunk_files(cdir: str, total_chunks: int) -> None:
    """Remove the numbered chunk files, keeping the session dir + meta.json."""
    if not isinstance(total_chunks, int):
        return
    for i in range(total_chunks):
        try:
            os.unlink(os.path.join(cdir, f'{i:06d}'))
        except OSError:
            pass


@contextlib.contextmanager
def _claim_flock(cdir: str):
    """Hold a cross-process advisory lock on a session for the claim window.

    gunicorn runs several worker processes, so the per-process ``_finalize_lock``
    cannot make the claim's read-modify-write atomic across them.  An flock on a
    per-session lock file does: a second worker (or thread) blocks here until the
    first finishes its short claim, then re-reads the now-updated meta and backs
    off.  The lock is advisory and released by the OS if the holder dies, so a
    crashed owner never wedges a re-drive.  flock is POSIX/local-filesystem;
    this assumes the documented single-host web deployment.  Yields True when the
    lock was acquired, False when it could not be (best-effort fall back to the
    in-process lock only).
    """
    fd = None
    try:
        fd = os.open(os.path.join(cdir, _LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    except OSError:
        yield False
    finally:
        if fd is not None:
            try:
                os.close(fd)  # releases the flock
            except OSError:
                pass


def _get_executor() -> ThreadPoolExecutor:
    """Lazily create the per-process finalise thread pool."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=finalize_concurrency(),
                    thread_name_prefix='finalize')
    return _executor


def finalize_status(upload_uuid: str) -> dict | None:
    """Return the finalise state for a session, or None if it does not exist.

    The returned dict always carries 'state' (one of the FINALIZE_* constants,
    or 'pending' when finalise has not been claimed yet) and, depending on
    state, 'artefact_uuid' (done) or 'error'/'error_code' (failed).
    """
    meta = read_meta(upload_uuid)
    if meta is None:
        return None
    state = meta.get('finalize_state') or 'pending'
    out = {'state': state}
    if state == FINALIZE_DONE:
        out['artefact_uuid'] = meta.get('artefact_uuid')
    elif state == FINALIZE_FAILED:
        out['error'] = meta.get('error')
        out['error_code'] = meta.get('error_code')
    return out


def finalize_is_stale(upload_uuid: str) -> bool:
    """True if an 'assembling' session's heartbeat is old enough to re-drive."""
    meta = read_meta(upload_uuid)
    if meta is None or meta.get('finalize_state') != FINALIZE_ASSEMBLING:
        return False
    return (time.time() - meta.get('heartbeat_at', 0)) > finalize_stale_seconds()


def claim_finalize(upload_uuid: str) -> bool:
    """Atomically claim a session for finalise, transitioning it to 'assembling'.

    Returns True to the single caller that wins the claim; that caller must then
    submit_finalize().  Claimable when the session is not yet finalised
    (no finalize_state / 'pending') or is a stale 'assembling' (the previous
    runner died — a redeploy or crash).  A live 'assembling', 'done' or 'failed'
    session is not claimable.  This single-winner transition is what prevents a
    double finalise (and hence duplicate artefacts).

    Atomicity holds across gunicorn worker *processes*: the read-modify-write
    runs under a per-session flock (_claim_flock), so a second worker serialises
    behind the first and then observes the just-written 'assembling' + fresh
    heartbeat and backs off.  The in-process _finalize_lock additionally orders
    it against the heartbeat writer.
    """
    cdir = chunk_dir(upload_uuid)
    stale_seconds = finalize_stale_seconds()
    with _claim_flock(cdir), _finalize_lock:
        meta = _read_meta_dir(cdir)
        if meta is None:
            return False
        state = meta.get('finalize_state')
        if state == FINALIZE_ASSEMBLING:
            if (time.time() - meta.get('heartbeat_at', 0)) <= stale_seconds:
                return False  # a live runner holds it
            # else: stale heartbeat — the previous runner is gone, re-drive
        elif state in (FINALIZE_DONE, FINALIZE_FAILED):
            return False
        # state is None / 'pending' (fresh) or stale 'assembling' (re-drive)
        now = time.time()
        meta['finalize_state'] = FINALIZE_ASSEMBLING
        meta['heartbeat_at'] = now
        meta['attempts'] = meta.get('attempts', 0) + 1
        meta.pop('error', None)
        meta.pop('error_code', None)
        _write_meta_dir(cdir, meta)
        return True


def _set_finalize_result(cdir: str, **fields) -> None:
    """Merge result fields into meta.json under the finalise lock."""
    with _finalize_lock:
        meta = _read_meta_dir(cdir)
        if meta is None:
            return
        meta.update(fields)
        _write_meta_dir(cdir, meta)


def _heartbeat_loop(cdir: str, interval: int, stop: threading.Event) -> None:
    """Refresh heartbeat_at every *interval* seconds until *stop* is set.

    Runs in its own thread without an app context, operating on the already
    resolved session directory, so a live finalise never looks stale to a
    concurrent re-drive check.
    """
    while not stop.wait(interval):
        with _finalize_lock:
            meta = _read_meta_dir(cdir)
            if meta is None or meta.get('finalize_state') != FINALIZE_ASSEMBLING:
                return
            meta['heartbeat_at'] = time.time()
            _write_meta_dir(cdir, meta)


def run_finalize(upload_uuid: str, finalize_fn) -> None:
    """Assemble, ingest, and record the outcome for a claimed session.

    *finalize_fn(assembled)* performs the blueprint-specific ingest (resolving
    the item, calling ingest_uploaded_artefact, applying the right auth/queue
    mode) and returns the created artefact's UUID string.  Must be called only
    after a successful claim_finalize(); runs inside an app context.
    """
    cdir = chunk_dir(upload_uuid)
    meta = read_meta(upload_uuid)
    if meta is None:
        return
    total_chunks = meta.get('total_chunks')
    original_filename = meta.get('filename') or 'unnamed'

    stop = threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop,
        args=(cdir, finalize_heartbeat_seconds(), stop),
        daemon=True)
    hb.start()
    try:
        assembled = assemble_to_storage(
            upload_uuid, original_filename,
            total_chunks=total_chunks, max_size=max_upload_size(),
            cleanup=False)
        artefact_uuid = finalize_fn(assembled)
        _set_finalize_result(
            cdir, finalize_state=FINALIZE_DONE,
            artefact_uuid=artefact_uuid, finalized_at=time.time())
    except UploadTooLarge as exc:
        _set_finalize_result(
            cdir, finalize_state=FINALIZE_FAILED,
            error=str(exc), error_code='too_large', finalized_at=time.time())
    except StorageUnavailable as exc:
        _set_finalize_result(
            cdir, finalize_state=FINALIZE_FAILED,
            error=f'Storage backend unavailable: {exc}', error_code='storage',
            finalized_at=time.time())
    except Exception:
        current_app.logger.exception(
            'Chunked-upload finalise failed for session %s', upload_uuid)
        _set_finalize_result(
            cdir, finalize_state=FINALIZE_FAILED,
            error='Internal error during finalise', error_code='internal',
            finalized_at=time.time())
    finally:
        stop.set()
        # Once finalise reaches a terminal state (done or failed — neither is
        # re-claimable), the staged chunks are dead weight; drop them now rather
        # than leaving up to max_upload_size bytes on the volume until the TTL
        # purge.  meta.json is retained for status polling.
        _delete_chunk_files(cdir, total_chunks)
        purge_stale_chunks()


def submit_finalize(upload_uuid: str, finalize_fn) -> None:
    """Submit a claimed session's finalise to the thread pool.

    Captures the real app object so the pool thread can push its own app
    context (the request context that issued the submit will be gone).
    """
    app = current_app._get_current_object()

    def _job():
        with app.app_context():
            try:
                run_finalize(upload_uuid, finalize_fn)
            except Exception:
                app.logger.exception(
                    'Unhandled error in finalise job %s', upload_uuid)

    _get_executor().submit(_job)


def shutdown_executor(wait: bool = False) -> None:
    """Tear down the finalise pool (used by tests).  Orphaned jobs re-drive."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None

# vim: ts=4 sw=4 et
