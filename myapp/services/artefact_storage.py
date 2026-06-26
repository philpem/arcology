"""Arcology - Artefact storage service

Storage-backend paths, keys, hashing and file resolution for artefacts and
their extracted files.  Shared by the web blueprints, the REST API, and the
Flask CLI commands — none of which should reach into another blueprint's
internals for this logic.

Moved verbatim from myapp/blueprints/artefacts.py.
"""

import glob
import hashlib
import os
import re
import tempfile
import uuid
from flask import current_app
from werkzeug.utils import secure_filename
from ..database import (
    Analysis,
    AnalysisStatus,
    AnalysisType,
    Artefact,
    StorageDirectory,
)
from ..utils.blobs import artefact_blob


def safe_original_filename(filename: str) -> str:
    """
    Sanitize a filename for safe storage as original_filename.

    Unlike Werkzeug's secure_filename(), this preserves characters found in
    RISC OS filenames, notably the comma used for filetype suffixes
    (e.g. ``CF-D1,FCD`` where ``,FCD`` encodes RISC OS filetype &FCD).

    Path separators, null bytes and the other C0 control characters (notably
    CR and LF) plus DEL are stripped: they have no legitimate place in a
    filename and, if preserved, would flow into HTTP response headers such as
    Content-Disposition and enable header injection (CWE-113).  Every other
    character — including the top-bit-set range 0x80-0xFF — is kept as-is so
    the original name is faithfully recorded.

    Crucially, code points 0x80-0x9F are NOT stripped: ISO 8859-1 treats them
    as C1 control codes, but RISC OS assigns them printable glyphs (Euro,
    ligatures, etc. — see shared RISC OS Latin-1 handling), and 0xA0 is the
    Acorn hard space.  Removing them would corrupt RISC OS filenames.  These
    bytes cannot split an HTTP header (only CR/LF do), and the
    Content-Disposition sink encodes them safely via RFC 5987 filename*.
    """
    # Drop path separators and the C0 control range (0x00-0x1f, includes
    # NUL/CR/LF) plus DEL (0x7f).  Preserve 0x80+ for RISC OS filenames.
    filename = ''.join(
        ch for ch in filename
        if ch not in ('/', '\\') and ord(ch) >= 0x20 and ord(ch) != 0x7f
    )
    filename = filename.strip()
    return filename or 'upload'


def get_upload_folder():
    """Get the upload folder path, creating it if necessary."""
    folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    if not os.path.isabs(folder):
        folder = os.path.join(current_app.instance_path, folder)
    os.makedirs(folder, exist_ok=True)
    return folder


def get_output_folder():
    """Get the output folder path, creating it if necessary."""
    folder = current_app.config.get('OUTPUT_FOLDER', 'outputs')
    if not os.path.isabs(folder):
        folder = os.path.join(current_app.instance_path, folder)
    os.makedirs(folder, exist_ok=True)
    return folder


def get_storage_extension(filename: str) -> str:
    """
    Get the full extension for storage, preserving compound extensions.

    For compound extensions like .dd.zst, .dd.gz, .dd.bz2, .tar.gz,
    returns the full compound extension instead of just the last part.
    E.g., 'drive.dd.zst' -> '.dd.zst' (not '.zst').
    """
    filename_lower = filename.lower()
    for compound_ext in ('.dd.zst', '.dd.gz', '.dd.bz2', '.tar.gz'):
        if filename_lower.endswith(compound_ext):
            return filename[-len(compound_ext):]
    _, ext = os.path.splitext(filename)
    return ext


def save_uploaded_file(file) -> tuple[str, int]:
    """
    Save an uploaded file and return (storage_path, file_size).
    Files are stored via the storage backend with a UUID-based name to avoid conflicts.
    """
    storage = current_app.storage

    # Generate unique storage name while preserving extension
    # Uses compound extension detection so drive.dd.zst -> <uuid>.dd.zst
    original_name = secure_filename(file.filename)
    ext = get_storage_extension(original_name)
    storage_name = f"{uuid.uuid4().hex}{ext}"

    # Save to a temp file first, then put into storage
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        file.save(tmp)
        tmp_path = tmp.name

    try:
        file_size = os.path.getsize(tmp_path)
        key = storage.storage_key('uploads', storage_name)
        storage.put(key, tmp_path)
    finally:
        # Clean up temp file (if storage backend copied it elsewhere)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    # Return relative path for storage in DB
    return storage_name, file_size


def get_artefact_storage_key(artefact: Artefact) -> str:
    """Get the storage key for an artefact.

    Returns a key like 'uploads/abc123.img' or 'outputs/xyz789.png'.
    Uses the blob's physical storage path when a blob record exists; falls
    back to artefact.storage_path for legacy (pre-blob) artefacts.
    """
    blob = artefact_blob(artefact)
    storage_path = blob.storage_path if blob is not None else artefact.storage_path
    directory = 'outputs' if artefact.storage_directory == StorageDirectory.OUTPUTS else 'uploads'
    return current_app.storage.storage_key(directory, storage_path)


def get_artefact_path(artefact: Artefact) -> str:
    """Get the full filesystem path for an artefact based on its storage directory.

    Raises ValueError if storage_path would escape the storage directory
    (absolute path override or directory traversal).
    """
    if artefact.storage_directory == StorageDirectory.OUTPUTS:
        folder = get_output_folder()
    else:
        folder = get_upload_folder()
    base = os.path.realpath(folder)
    full_path = os.path.realpath(os.path.join(folder, artefact.storage_path))
    if not (full_path == base or full_path.startswith(base + os.sep)):
        raise ValueError(f"storage_path escapes storage directory: {artefact.storage_path!r}")
    return full_path


def compute_file_hashes(filepath_or_key: str, use_storage: bool = False,
                        with_size: bool = False):
    """Compute MD5 and SHA256 hashes for a file.

    Args:
        filepath_or_key: Either a local filesystem path or a storage key.
        use_storage: If True, read from the storage backend using key.
        with_size: If True, also return the byte count as a third tuple element
            ``(md5, sha256, size)``, counted in the same single pass so a caller
            needing a blob's ``(file_size, sha256)`` avoids a second read.
            Default returns ``(md5, sha256)`` for existing callers.
    """
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()
    size = 0

    if use_storage:
        f = current_app.storage.open_read(filepath_or_key)
    else:
        f = open(filepath_or_key, 'rb')

    try:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_hash.update(chunk)
            sha256_hash.update(chunk)
            size += len(chunk)
    finally:
        f.close()

    if with_size:
        return md5_hash.hexdigest(), sha256_hash.hexdigest(), size
    return md5_hash.hexdigest(), sha256_hash.hexdigest()


def resolve_extracted_file_path(ef):
    """Resolve an ExtractedFile to its actual path on disk.

    Looks up completed FILE_EXTRACTION and ARCHIVE_EXTRACT analyses for the
    file's partition's artefact, then joins their output_path with the
    file's on-disk relative path.  Handles RISC OS filetype suffix fallback
    via glob.

    For files extracted from a nested archive (e.g. a zip inside a disc
    image), the API prepends the parent archive's display path to ef.path so
    the file appears nested in the UI.  That prefix is stripped here before
    joining, because the actual files live in a separate ARCHIVE_EXTRACT
    output directory, not under the disc's FILE_EXTRACTION tree.

    When the worker runs in a different container, the stored output_path
    may be an absolute path that doesn't exist on the web host (e.g.
    /data/outputs/... vs /app/instance/outputs/...).  As a fallback, the
    relative directory structure is resolved under OUTPUT_FOLDER.

    Returns the full filesystem path as a string, or None if not found.
    """
    from arcology_shared.storage import S3Storage

    storage = current_app.storage

    # For files nested inside an archive-within-a-disc, the DB path has the
    # parent archive's display path prepended (e.g. "Archives/Emulators.zip/
    # Docs/file.txt").  The actual files are in the ARCHIVE_EXTRACT output
    # for that inner zip, so we strip the prefix to get the real disk path.
    #
    # However, RISC OS archives often contain a single top-level directory
    # whose name matches the archive filename (e.g. archive "QRT" contains
    # "QRT/Documents/!ReadMe" internally).  In that case the extractor
    # preserves the "QRT/" subdirectory, so the on-disk path relative to the
    # ARCHIVE_EXTRACT output is still "QRT/Documents/!ReadMe" — stripping
    # "QRT/" produces the wrong path.  We therefore try both the stripped
    # and the original (unstripped) DB path so both layouts are handled.
    original_disk_path = ef.path
    disk_path = ef.path
    if ef.parent_file_id:
        parent = ef.parent_file
        if parent and parent.is_archive:
            strip_prefix = parent.path + '/'
            if disk_path.startswith(strip_prefix):
                disk_path = disk_path[len(strip_prefix):]

    # Stripped form first (handles extractors that put files directly in
    # output_dir); unstripped fallback for archives with a matching top-level
    # directory (the coincidence case described above).
    disk_paths_to_try = [disk_path]
    if disk_path != original_disk_path:
        disk_paths_to_try.append(original_disk_path)

    for analysis_type in (AnalysisType.FILE_EXTRACTION, AnalysisType.ARCHIVE_EXTRACT):
        # Use .all() so we try every extraction output for this artefact.
        # A disc image may have multiple nested archives, each with its own
        # ARCHIVE_EXTRACT and output_path; .first() would pick an arbitrary
        # one that may not contain this particular file.
        extractions = (
            Analysis.query
            .filter_by(artefact_id=ef.partition.artefact_id, analysis_type=analysis_type)
            .filter(Analysis.output_path.isnot(None), Analysis.status == AnalysisStatus.COMPLETED)
            .all()
        )
        for extraction in extractions:
            base = extraction.output_path

            # --- S3 storage mode ---
            if isinstance(storage, S3Storage):
                from botocore.exceptions import ClientError

                # output_path should be a relative path (or S3 key prefix)
                if os.path.isabs(base):
                    # Legacy absolute path — strip to relative
                    # e.g. /data/outputs/item/art/analysis -> item/art/analysis
                    parts = base.rstrip('/').split('/')
                    # Find 'outputs' in the path and take everything after
                    try:
                        idx = parts.index('outputs')
                        base = '/'.join(parts[idx + 1:])
                    except ValueError:
                        # No 'outputs' segment, use the last 3 parts as best guess
                        base = '/'.join(parts[-3:]) if len(parts) >= 3 else parts[-1]

                s3_prefix = f"outputs/{base.strip('/')}"

                # Build candidate keys.  If the file has a known RISC OS
                # filetype, try ,xxx suffix variants (both cases) before
                # the plain key.  This avoids an expensive list_prefix()
                # call — each HEAD/GET is ~12x cheaper than a LIST on AWS.
                # Try each disk_path variant in order (stripped first, then
                # unstripped fallback for archives with matching top-level dir).
                s3_candidates = []
                for dp in disk_paths_to_try:
                    file_key = f"{s3_prefix}/{dp.lstrip('/')}"
                    if ef.risc_os_filetype:
                        s3_candidates.append(file_key + ',' + ef.risc_os_filetype.lower())
                        s3_candidates.append(file_key + ',' + ef.risc_os_filetype.upper())
                    s3_candidates.append(file_key)

                for key in s3_candidates:
                    try:
                        tmp_dir = tempfile.mkdtemp(prefix='arcology_ef_')
                        dest = os.path.join(tmp_dir, key.rsplit('/', 1)[-1])
                        storage.get(key, dest)
                        return dest
                    except (FileNotFoundError, ClientError) as e:
                        # storage.get() raises FileNotFoundError for S3 404s;
                        # catch ClientError too for direct botocore errors.
                        if isinstance(e, ClientError) and e.response['Error']['Code'] not in ('404', 'NoSuchKey'):
                            raise
                        # Clean up empty temp dir
                        try:
                            os.unlink(dest)
                        except OSError:
                            pass
                        try:
                            os.rmdir(tmp_dir)
                        except OSError:
                            pass
                        continue

                continue

            # --- Local storage mode ---
            output_folder = get_output_folder()
            real_output = os.path.realpath(output_folder)

            # Build candidate base directories to try
            bases_to_try = []
            if os.path.isabs(base):
                bases_to_try.append(base)
                # Cross-container fallback: try relative subpath under OUTPUT_FOLDER.
                # Worker stores e.g. "/data/outputs/item/art/analysis/" but the web
                # app sees the same files at "/app/instance/outputs/item/art/analysis/".
                # Walk up the stored path to find the deepest suffix that exists
                # under our output_folder.
                parts = base.rstrip('/').split('/')
                for i in range(1, len(parts)):
                    candidate = os.path.join(output_folder, *parts[i:])
                    if os.path.isdir(candidate):
                        bases_to_try.append(candidate)
                        break
            else:
                bases_to_try.append(os.path.join(output_folder, base))

            for resolved_base in bases_to_try:
                real_base = os.path.realpath(resolved_base)
                # Ensure the base directory itself lies within OUTPUT_FOLDER.
                # Without this check, a malicious output_path (e.g. /etc) would
                # pass the inner confinement test and expose arbitrary host files.
                if not real_base.startswith(real_output + os.sep):
                    continue
                for dp in disk_paths_to_try:
                    raw_path = os.path.join(resolved_base, dp.lstrip('/'))
                    file_path = os.path.realpath(raw_path)
                    if not file_path.startswith(real_base + os.sep):
                        continue
                    if os.path.isfile(file_path):
                        return file_path
                    # RISC OS filetype suffix fallback — re-check confinement on
                    # each candidate so a glob match cannot escape real_base.
                    # Restrict to files whose comma-suffix is 1–3 hex digits so
                    # that DIM sidecar files (e.g. filename,INF) are never served
                    # in place of the actual data file.
                    candidates = [
                        f for f in glob.glob(raw_path + ',*')
                        if os.path.isfile(f)
                        and os.path.realpath(f).startswith(real_base + os.sep)
                        and re.search(r',[0-9a-fA-F]{1,3}$', os.path.basename(f))
                    ]
                    if candidates:
                        return candidates[0]

    return None


def map_output_path_to_local_root(path: str, output_folder: str) -> str:
    """Map a stored output path into the local OUTPUT_FOLDER namespace.

    Worker analyses may store absolute paths rooted at the worker container's
    OUTPUT_DIR (for example ``/data/outputs/...``), while the web app sees the
    same files under its own mount point (for example
    ``/app/instance/outputs/...``). This helper rewrites the absolute path onto
    the local output root before cleanup-time safety checks.

    Relative paths (used in S3 mode) are joined with output_folder directly.
    """
    real_output = os.path.realpath(output_folder)

    # Relative paths: join directly with output_folder
    if not os.path.isabs(path):
        return os.path.realpath(os.path.join(real_output, path))

    real_path = os.path.realpath(path)

    if real_path == real_output or real_path.startswith(real_output + os.sep):
        return real_path

    # Cross-container fallback: re-root the path under our local OUTPUT_FOLDER
    # using the suffix after the worker's output-root basename ("outputs").
    output_root_name = os.path.basename(real_output.rstrip(os.sep))
    parts = [part for part in path.rstrip(os.sep).split(os.sep) if part]
    if output_root_name in parts:
        suffix = parts[parts.index(output_root_name) + 1:]
        if suffix:
            return os.path.realpath(os.path.join(real_output, *suffix))

    return real_path

# vim: ts=4 sw=4 et
