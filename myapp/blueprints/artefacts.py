"""
Arcology - Artefacts Blueprint

CRUD operations for digital artefacts with file upload and auto-analysis.
"""

import glob
import os
import re
import hashlib
import shutil
import tempfile
import threading
import uuid
import json
import mimetypes
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, send_file, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import StringField, TextAreaField, SelectField, BooleanField, IntegerField
from wtforms.validators import DataRequired, Optional
from werkzeug.utils import secure_filename

from ..extensions import db
from ..permissions import require_permission
from ..utils.slugs import generate_slug, ensure_unique_slug, lookup_by_identifier, lookup_artefact_by_id
from ..riscos_filetypes import lookup_filetype_hex


def safe_original_filename(filename: str) -> str:
    """
    Sanitize a filename for safe storage as original_filename.

    Unlike Werkzeug's secure_filename(), this preserves characters found in
    RISC OS filenames, notably the comma used for filetype suffixes
    (e.g. ``CF-D1,FCD`` where ``,FCD`` encodes RISC OS filetype &FCD).

    Path separators and null bytes are stripped to prevent directory
    traversal; everything else is kept as-is so the original name is
    faithfully recorded.
    """
    # Strip null bytes and path separators (security-critical)
    for ch in ('\x00', '/', '\\'):
        filename = filename.replace(ch, '')
    filename = filename.strip()
    return filename or 'upload'

from ..database import (
    Item, Artefact, ArtefactType, Partition, ExtractedFile,
    Analysis, AnalysisType, AnalysisStatus, Platform, StorageDirectory, Tag,
    ArtefactProtection, ArtefactMastering,
    HashDatabase, KnownProduct, KnownFile, RecognisedProduct,
    RiscosModule, ArtefactRestriction, ExtractedFileRestriction, artefact_tags,
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='', template_folder='templates')


# =============================================================================
# Type Detection
# =============================================================================

# Extension to ArtefactType mapping
EXTENSION_MAP = {
    # Flux-level
    '.scp': ArtefactType.SCP,
    '.dfi': ArtefactType.DFI,
    '.a2r': ArtefactType.A2R,

    # Cooked sector-level floppy or hard disc
    '.imd': ArtefactType.IMD,   # needs conversion to sectors
    '.hfe': ArtefactType.HFE,   # needs conversion to sectors

    # Raw sector images
    '.adf': ArtefactType.RAW_SECTOR,
    '.img': ArtefactType.RAW_SECTOR,
    '.ima': ArtefactType.RAW_SECTOR,
    '.dsk': ArtefactType.RAW_SECTOR,
    
    # CD/DVD
    '.iso': ArtefactType.ISO,
    
    # Hard drive raw images
    '.dd': ArtefactType.RAW_SECTOR,
    
    # Documents
    '.pdf': ArtefactType.PDF,
        
    # Archives
    '.zip': ArtefactType.ZIP,
    '.tar.gz': ArtefactType.TARGZ,
    '.tgz': ArtefactType.TARGZ,
    '.rar': ArtefactType.RAR,
    '.arc': ArtefactType.ARC,
    '.arcfs': ArtefactType.ARC,
    '.spk': ArtefactType.ARC,
    '.spark': ArtefactType.ARC,
    '.b21':   ArtefactType.TBAFS,
    '.tbafs': ArtefactType.TBAFS,
    '.b23':   ArtefactType.XFILES,

    # Acorn/RISC OS native viewable formats
    '.spr':  ArtefactType.ACORN_SPRITE,
    '.aff':  ArtefactType.ACORN_DRAW,
    '.draw': ArtefactType.ACORN_DRAW,
    '.txt':  ArtefactType.ACORN_TEXT,
}


def detect_artefact_type(filename: str) -> ArtefactType:
    """Detect artefact type from filename extension."""
    filename_lower = filename.lower()

    # Check compound extensions first (order matters)
    if filename_lower.endswith('.dd.zst'):
        return ArtefactType.DD_ZST
    if filename_lower.endswith('.dd.gz'):
        return ArtefactType.DD_GZ
    if filename_lower.endswith('.dd.bz2'):
        return ArtefactType.DD_BZ2
    if filename_lower.endswith('.tar.gz'):
        return ArtefactType.TARGZ

    # Strip a trailing compression suffix and re-check, so e.g. .dfi.bz2 → .dfi
    stem = filename_lower
    for suffix in ('.gz', '.bz2', '.zst'):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    _, ext = os.path.splitext(stem)
    return EXTENSION_MAP.get(ext, ArtefactType.UNKNOWN)


# Analysis types appropriate for each artefact type
ANALYSIS_MAP = {
    # Flux images - visualisation and decode attempt
    ArtefactType.SCP: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    ArtefactType.DFI: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    ArtefactType.A2R: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    #ArtefactType.KF: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    #ArtefactType.FLUX_RAW: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE],
    
    # Sector-level floppy - file extraction only works on raw sector images
    # IMD is track-based format with metadata, HFE is an emulator container format
    # These need conversion to IMG (raw sectors) before file extraction can work.
    # FLUX_DECODE is included so that standalone HFE/IMD uploads trigger extraction
    # (same pipeline as SCP, starting from wherever in the chain the source sits).
    ArtefactType.IMD: [AnalysisType.METADATA_EXTRACT, AnalysisType.FLUX_DECODE],
    ArtefactType.HFE: [AnalysisType.DISC_MASTERING_DETECT, AnalysisType.DISC_PROTECTION_DETECT, AnalysisType.FLUX_DECODE],
    #ArtefactType.TD0: [AnalysisType.METADATA_EXTRACT, AnalysisType.FORMAT_IDENTIFY],
    #ArtefactType.D64: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.FILE_EXTRACTION],
    #ArtefactType.ADF: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.FILE_EXTRACTION],
    #ArtefactType.DSK: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.FILE_EXTRACTION],
    
    # CD/DVD - file extraction
    ArtefactType.ISO: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_EXTRACTION],
    #ArtefactType.BIN_CUE: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_EXTRACTION],
    #ArtefactType.MDF_MDS: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_EXTRACTION],
    #ArtefactType.NRG: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_EXTRACTION],
    
    # Raw sector images - run PARTITION_DETECT first; it queues FILE_EXTRACTION
    # with the detected filesystem hint so the right tool (DIM vs 7z) is used.
    # FILE_EXTRACTION must NOT be queued here directly, as it would race with
    # PARTITION_DETECT and fall back to the wrong tool (7z for ADFS discs, etc.).
    #ArtefactType.RAW_SECTOR: [AnalysisType.PARTITION_DETECT, AnalysisType.FORMAT_IDENTIFY],
    ArtefactType.RAW_SECTOR: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_ZST: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_GZ: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_BZ2: [AnalysisType.PARTITION_DETECT],
    
    # Documents/images - just metadata/checksums
    ArtefactType.PDF: [AnalysisType.METADATA_EXTRACT],
    
    # Archives - extract contents via ARCHIVE_EXTRACT (same pipeline used
    # for archives found inside disc images).  The worker detects top-level
    # artefact archives (no partition_uuid hint) and extracts them directly.
    ArtefactType.ZIP: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.TARGZ: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.RAR: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.ARC: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.TBAFS:  [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.XFILES: [AnalysisType.ARCHIVE_EXTRACT],

    # Acorn/RISC OS native viewable formats — convert to portable equivalents
    ArtefactType.ACORN_SPRITE: [AnalysisType.FORMAT_CONVERT],
    ArtefactType.ACORN_DRAW:   [AnalysisType.FORMAT_CONVERT],
    ArtefactType.ACORN_TEXT:   [AnalysisType.FORMAT_CONVERT],

    # Unknown - try to identify
    ArtefactType.UNKNOWN: [AnalysisType.FORMAT_IDENTIFY],
}


def queue_analyses_for_artefact(artefact: Artefact, hints: dict = None,
                                checksum_only: bool = False,
                                skip_duplicate_check: bool = False,
                                commit: bool = True,
                                skip_analyses: list[str] | None = None):
    """Queue appropriate analyses for an artefact based on its type.

    CHECKSUM_COMPUTE is always prepended as the first job regardless of artefact
    type; it does not need to appear in ANALYSIS_MAP.  Pass checksum_only=True
    to skip the type-specific analyses (used when auto-analyse is off on upload).

    When called after reset_artefact_for_reanalysis, pass skip_duplicate_check=True
    to avoid redundant SELECT queries (the reset already deleted all analyses).

    Pass commit=False to defer the commit to the caller (useful for batch operations).

    skip_analyses: list of AnalysisType *names* (uppercase strings, e.g. 'FLUX_DECODE')
    to suppress.  Used when registering siblings that must not re-trigger the
    parent analysis (ping-pong prevention).
    """
    skip_set = set(skip_analyses or [])
    analysis_types = [AnalysisType.CHECKSUM_COMPUTE]
    if not checksum_only:
        analysis_types += ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])
    analysis_types = [t for t in analysis_types if t.name not in skip_set]
    hints_json = json.dumps(hints) if hints else None

    for analysis_type in analysis_types:
        if not skip_duplicate_check:
            # Check if this analysis is already queued/running
            existing = Analysis.query.filter_by(
                artefact_id=artefact.id,
                analysis_type=analysis_type
            ).filter(Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING])).first()
            if existing:
                continue

        analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=analysis_type,
                status=AnalysisStatus.PENDING,
                hints=hints_json
            )
        db.session.add(analysis)

    if commit:
        db.session.commit()


# =============================================================================
# Forms
# =============================================================================

class ArtefactUploadForm(FlaskForm):
    """Form for uploading a new artefact."""
    file = FileField('File', validators=[FileRequired()])
    label = StringField('Label', validators=[DataRequired()],
                        description='e.g., "Disc 1", "Program Disc", "Manual"')
    platform_id = SelectField('Platform hint', coerce=int, validators=[Optional()],
                               description='Helps analysis tools identify format')
    dfi_clock_mhz = IntegerField('DFI clock frequency (MHz)', validators=[Optional()],
                                  description='Override sample frequency for DFI files recorded at non-standard rates (e.g. 100)')
    artefact_type = SelectField('Type (auto-detected)', coerce=str, validators=[Optional()],
                                 description='Leave as "Auto-detect" unless incorrect')
    description = TextAreaField('Description', validators=[Optional()])
    auto_analyse = BooleanField('Run automatic analysis', default=True)
    upload_more = BooleanField('Upload more', default=False)


class ArtefactEditForm(FlaskForm):
    """Form for editing artefact metadata."""
    label = StringField('Label', validators=[DataRequired()])
    artefact_type = SelectField('Type', coerce=str, validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    tags = StringField('Tags', validators=[Optional()],
                       description='Comma-separated list of tags')


class AnalyseForm(FlaskForm):
    """Form for running analysis with optional hints."""
    platform_id = SelectField('Platform hint', coerce=int, validators=[Optional()],
                               description='Helps analysis tools identify format')
    filesystem_hint = StringField('Filesystem hint', validators=[Optional()],
                                   description='e.g., adfs, fat12, hfs')
    dfi_clock_mhz = IntegerField('DFI clock frequency (MHz)', validators=[Optional()],
                                  description='Override sample frequency for DFI files recorded at non-standard rates (e.g. 100)')
    notes = TextAreaField('Additional notes', validators=[Optional()])


class FileSearchForm(FlaskForm):
    partition_uuid = StringField('Partition UUID', validators=[Optional()])
    filename = StringField('Filename', validators=[Optional()])
    filetype = StringField('Filetype', validators=[Optional()])
    path = StringField('Path/Directory', validators=[Optional()])
    md5 = StringField('MD5 Hash', validators=[Optional()])
    sha1 = StringField('SHA1 Hash', validators=[Optional()])
    hide_known = SelectField('Known files', choices=[
        ('', 'Known: All'),
        ('hide', 'Known: Hide'),
        ('only', 'Known: Only'),
    ], default='', validators=[Optional()])
    filter_products = SelectField('Product matches', choices=[
        ('', 'Products: All'),
        ('hide', 'Products: Hide'),
        ('only', 'Products: Only'),
    ], default='', validators=[Optional()])
    show_directories = BooleanField('Show Dirs', default=False)


# =============================================================================
# Helper Functions
# =============================================================================

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


def _get_storage_extension(filename: str) -> str:
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
    ext = _get_storage_extension(original_name)
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
    """
    directory = 'outputs' if artefact.storage_directory == StorageDirectory.OUTPUTS else 'uploads'
    return current_app.storage.storage_key(directory, artefact.storage_path)


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


def compute_file_hashes(filepath_or_key: str, use_storage: bool = False) -> tuple[str, str]:
    """Compute MD5 and SHA256 hashes for a file.

    Args:
        filepath_or_key: Either a local filesystem path or a storage key.
        use_storage: If True, read from the storage backend using key.
    """
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()

    if use_storage:
        f = current_app.storage.open_read(filepath_or_key)
    else:
        f = open(filepath_or_key, 'rb')

    try:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_hash.update(chunk)
            sha256_hash.update(chunk)
    finally:
        f.close()

    return md5_hash.hexdigest(), sha256_hash.hexdigest()


def _resolve_extracted_file_path(ef):
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
    from shared.storage import S3Storage

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


def _map_output_path_to_local_root(path: str, output_folder: str) -> str:
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


# =============================================================================
# Routes
# =============================================================================

def get_all_derived_artefact_ids(artefact: Artefact) -> list[int]:
    """Collect all derived artefact IDs using a single recursive CTE query.

    Replaces the previous recursive ORM walk which triggered N+1 queries
    (one per level of the derivation tree).
    """
    from sqlalchemy import literal_column, union_all, select
    from ..extensions import db

    base = select(Artefact.id).where(Artefact.parent_artefact_id == artefact.id)
    cte = base.cte(name='derived', recursive=True)
    recursive = select(Artefact.id).where(Artefact.parent_artefact_id == cte.c.id)
    cte = cte.union_all(recursive)
    rows = db.session.execute(select(cte.c.id)).all()
    return [r[0] for r in rows]


def _collect_all_analyses(artefact: Artefact) -> list:
    """Collect all analyses for an artefact and its derived artefacts.

    Uses the CTE-based get_all_derived_artefact_ids to avoid N+1 queries,
    then fetches all analyses in a single query.
    """
    from ..extensions import db
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    return Analysis.query.filter(Analysis.artefact_id.in_(all_ids)).order_by(Analysis.id.desc()).all()


def _bulk_delete_artefact_dependents(artefact_ids: list[int]):
    """Bulk-delete all referencing rows across every FK table for the given artefact IDs.

    Deletes analyses, extracted file restrictions, recognised products,
    extracted files, partitions, protection/mastering/module records,
    artefact restrictions, and tag associations.  Does NOT delete the
    artefacts themselves — call ``_bulk_delete_artefacts`` for that.
    """
    Analysis.query.filter(Analysis.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    partition_subq = db.session.query(Partition.id).filter(
        Partition.artefact_id.in_(artefact_ids)).subquery()
    ef_subq = db.session.query(ExtractedFile.id).filter(
        ExtractedFile.partition_id.in_(db.session.query(partition_subq.c.id))).subquery()
    ExtractedFileRestriction.query.filter(
        ExtractedFileRestriction.extracted_file_id.in_(
            db.session.query(ef_subq.c.id)
        )).delete(synchronize_session=False)
    RecognisedProduct.query.filter(
        RecognisedProduct.partition_id.in_(
            db.session.query(partition_subq.c.id)
        )).delete(synchronize_session=False)
    ExtractedFile.query.filter(
        ExtractedFile.partition_id.in_(
            db.session.query(partition_subq.c.id)
        )).delete(synchronize_session=False)
    Partition.query.filter(Partition.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactProtection.query.filter(ArtefactProtection.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactMastering.query.filter(ArtefactMastering.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    RiscosModule.query.filter(RiscosModule.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    ArtefactRestriction.query.filter(ArtefactRestriction.artefact_id.in_(artefact_ids)).delete(synchronize_session=False)
    db.session.execute(artefact_tags.delete().where(artefact_tags.c.artefact_id.in_(artefact_ids)))


def _bulk_delete_artefacts(artefact_ids: list[int]):
    """Delete artefact rows after their dependents have been removed.

    Breaks the self-referential parent FK before deleting.
    """
    Artefact.query.filter(Artefact.id.in_(artefact_ids)).update(
        {Artefact.parent_artefact_id: None}, synchronize_session=False)
    Artefact.query.filter(Artefact.id.in_(artefact_ids)).delete(synchronize_session=False)


def _analysis_file_path(analysis, hint_file_map: dict) -> str | None:
    """Return the file/dir path for an analysis that operates on a specific
    extracted file, or None for analyses that have no path context.

    Path-bearing analyses:
      ARCHIVE_EXTRACT  — path of the archive file being extracted
                         (from hint file_id → ExtractedFile.path)
      ARCHIVE_DETECT   — path_prefix of the archive being scanned for
                         nested archives (empty → top-level, returns None)
      FORMAT_CONVERT   — path_prefix of the archive being converted
                         (empty → direct artefact convert, returns None)
      RISCOS_MODULE_PARSE — path_prefix of the archive context
                         (empty → top-level scan, returns None)
    """
    import json as _json
    from shared.enums import AnalysisType as _AT

    if not analysis.hints:
        return None
    try:
        h = _json.loads(analysis.hints)
    except Exception:
        return None

    atype = analysis.analysis_type
    if atype == _AT.ARCHIVE_EXTRACT:
        fid = h.get('file_id')
        if fid and fid in hint_file_map:
            return hint_file_map[fid]['path']
        return None
    if atype in (_AT.ARCHIVE_DETECT, _AT.FORMAT_CONVERT, _AT.RISCOS_MODULE_PARSE):
        prefix = h.get('path_prefix', '')
        return prefix if prefix else None
    return None


def _build_file_path_tree(path_analyses: list[tuple[str, object]]) -> dict:
    """Build a nested dict tree from [(path, analysis), ...].

    Each node: {'children': {name: node, ...}, 'analyses': [Analysis, ...]}
    The root node's children are the top-level path components.  Children
    dicts preserve insertion order and are later sorted by the template.
    """
    root: dict = {'children': {}, 'analyses': []}
    for path, analysis in path_analyses:
        parts = [p for p in path.split('/') if p]
        node = root
        for part in parts:
            if part not in node['children']:
                node['children'][part] = {'children': {}, 'analyses': []}
            node = node['children'][part]
        node['analyses'].append(analysis)
    return root


def _build_processing_tree(root: Artefact) -> tuple[dict, bool, dict, int]:
    """Build a nested tree structure for the processing tree view.

    Returns (tree_node, has_active_analyses, status_counts, total_count).
    Each tree_node is a dict:
      {
        'artefact':   Artefact,
        'analyses':   [Analysis, ...],   # non-path analyses (flat list)
        'path_tree':  dict | None,       # nested path tree (see _build_file_path_tree)
        'children':   [node, ...],       # derived artefact child nodes
      }

    Analyses that have a file-path context (ARCHIVE_EXTRACT with a file_id,
    ARCHIVE_DETECT / FORMAT_CONVERT with a path_prefix) are separated out of
    the flat 'analyses' list and placed in 'path_tree' so the template can
    render them as a hierarchical file-path tree.

    All data is fetched in flat queries (no N+1) and assembled in Python.
    """
    import json as _json
    from collections import defaultdict
    from shared.enums import AnalysisType as _AT

    all_ids = [root.id] + get_all_derived_artefact_ids(root)

    all_artefacts = Artefact.query.filter(Artefact.id.in_(all_ids)).all()
    artefact_map = {a.id: a for a in all_artefacts}

    children_map: dict[int, list] = defaultdict(list)
    for a in all_artefacts:
        if a.parent_artefact_id is not None:
            children_map[a.parent_artefact_id].append(a)

    all_analyses = (
        Analysis.query
        .filter(Analysis.artefact_id.in_(all_ids))
        .order_by(Analysis.id)
        .all()
    )

    analyses_map: dict[int, list] = defaultdict(list)
    for analysis in all_analyses:
        analyses_map[analysis.artefact_id].append(analysis)

    has_active = any(
        a.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)
        for a in all_analyses
    )

    status_counts = {s.value: 0 for s in AnalysisStatus}
    for a in all_analyses:
        status_counts[a.status.value] += 1
    total_count = len(all_analyses)

    # Resolve ARCHIVE_EXTRACT file_ids → ExtractedFile paths in one query.
    file_ids = []
    for analysis in all_analyses:
        if analysis.analysis_type == _AT.ARCHIVE_EXTRACT and analysis.hints:
            try:
                fid = _json.loads(analysis.hints).get('file_id')
                if fid:
                    file_ids.append(fid)
            except Exception:
                pass

    hint_file_map: dict[int, dict] = {}
    if file_ids:
        rows = (
            ExtractedFile.query
            .filter(ExtractedFile.id.in_(file_ids))
            .with_entities(ExtractedFile.id, ExtractedFile.path, ExtractedFile.filename)
            .all()
        )
        hint_file_map = {r.id: {'path': r.path, 'filename': r.filename} for r in rows}

    def _build(aid: int) -> dict:
        plain: list = []
        path_items: list[tuple[str, object]] = []
        for a in analyses_map.get(aid, []):
            p = _analysis_file_path(a, hint_file_map)
            if p is not None:
                path_items.append((p, a))
            else:
                plain.append(a)
        return {
            'artefact': artefact_map[aid],
            'analyses': plain,
            'path_tree': _build_file_path_tree(path_items) if path_items else None,
            'children': [
                _build(c.id)
                for c in sorted(children_map.get(aid, []), key=lambda x: x.id)
            ],
        }

    return _build(root.id), has_active, status_counts, total_count


def reset_artefact_for_reanalysis(artefact: Artefact, commit: bool = True):
    """
    Reset an artefact to its just-uploaded state ready for re-analysis.

    Deletes all analyses, derived artefacts, and partitions (with their
    extracted file listings) for this artefact, then removes the associated
    files from disk.  The artefact's own uploaded file is preserved.

    This must be called before queueing new analyses when the user triggers
    a re-analyse, so that stale results from previous runs are fully cleared.

    Pass commit=False to defer the commit to the caller (useful for batch
    operations).  The caller must call db.session.commit() afterwards.
    """
    cleanup = _collect_cleanup_paths_for_artefact(artefact, 'reset')

    # Delete storage files for all derived artefacts (recursively).
    # Must happen before the DB delete so we can still walk the ORM tree.
    for derived in artefact.derived_artefacts:
        _delete_artefact_files(derived)

    # Collect all derived artefact IDs (including nested) for bulk deletion.
    all_derived_ids = get_all_derived_artefact_ids(artefact)

    # Collect all artefact IDs to clean (derived + root) for bulk operations.
    all_ids = all_derived_ids + [artefact.id]

    # Null out derived_from_analysis_id before deleting analyses, to avoid
    # FK violations from artefacts -> analyses.
    if all_derived_ids:
        Artefact.query.filter(Artefact.id.in_(all_derived_ids)).update(
            {Artefact.derived_from_analysis_id: None}, synchronize_session=False)

    _bulk_delete_artefact_dependents(all_ids)

    # Delete derived artefacts: break self-referential FK, then delete.
    if all_derived_ids:
        _bulk_delete_artefacts(all_derived_ids)

    if commit:
        db.session.commit()

    return cleanup


def _cleanup_analysis_outputs(output_folder, output_files, output_dirs, cache_dir, logger):
    """Delete analysis output files and directories.

    Designed to run in a background daemon thread; all paths are passed as
    plain strings so no ORM session or Flask app context is required.
    """
    real_output = os.path.realpath(output_folder)

    def _is_safe(p: str) -> bool:
        """Return True only if p resolves to a path inside output_folder."""
        return os.path.realpath(p).startswith(real_output + os.sep)

    def _prune_empty_parents(path: str) -> None:
        """Remove empty parent directories up to, but not including, output_folder."""
        current = os.path.dirname(os.path.realpath(path))
        while current.startswith(real_output + os.sep):
            try:
                os.rmdir(current)
                logger.info(f"Deleted empty parent directory: {current}")
            except OSError:
                break
            current = os.path.dirname(current)

    # Remove named output files (e.g., flux visualisation PNGs).
    for filename in output_files:
        path = os.path.join(output_folder, filename)
        if not _is_safe(path):
            logger.warning(f"Skipping out-of-bounds output file: {filename!r}")
            continue
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Deleted output file: {filename}")
            except Exception as e:
                logger.warning(f"Failed to delete output file {filename}: {e}")

    # Remove extraction output directories (e.g., extracted disc file trees).
    for path in output_dirs:
        local_path = _map_output_path_to_local_root(path, output_folder)
        if not _is_safe(local_path):
            logger.warning(f"Skipping out-of-bounds output directory: {path!r}")
            continue
        if os.path.exists(local_path):
            try:
                shutil.rmtree(local_path)
                logger.info(f"Deleted output directory: {local_path}")
                _prune_empty_parents(local_path)
            except Exception as e:
                logger.warning(f"Failed to delete output directory {path}: {e}")

    # Remove cached decompressed partition images created by PARTITION_DETECT.
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            logger.info(f"Deleted partition cache: {cache_dir}")
        except Exception as e:
            logger.warning(f"Failed to delete partition cache {cache_dir}: {e}")


def _collect_cleanup_paths_for_artefact(artefact: Artefact, context: str = 'cleanup') -> dict[str, list[str] | str]:
    """Collect output files/directories/cache paths for an artefact tree."""
    output_folder = get_output_folder()
    all_analyses = _collect_all_analyses(artefact)
    output_dirs = [a.output_path for a in all_analyses if a.output_path]
    output_files = []

    for analysis in all_analyses:
        if analysis.details:
            try:
                details = json.loads(analysis.details)
                if 'outputs' in details and isinstance(details['outputs'], list):
                    for output in details['outputs']:
                        if 'filename' in output:
                            output_files.append(output['filename'])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                current_app.logger.warning(f"Failed to parse analysis details during {context}: {e}")

    cache_dir = os.path.join(output_folder, '.cache', artefact.uuid)
    return {
        'output_folder': output_folder,
        'output_files': output_files,
        'output_dirs': output_dirs,
        'cache_dir': cache_dir,
    }


def _cleanup_artefact_outputs(artefact: Artefact, logger) -> None:
    """Delete derived output files/directories for an artefact tree."""
    cleanup = _collect_cleanup_paths_for_artefact(artefact)
    _cleanup_analysis_outputs(
        cleanup['output_folder'],
        cleanup['output_files'],
        cleanup['output_dirs'],
        cleanup['cache_dir'],
        logger,
    )


def _resolve_artefact(item_id, artefact_id, root_id=None):
    """Lookup helper: resolve item + artefact, validate root_id if nested URL."""
    item = lookup_by_identifier(Item, item_id)
    artefact = lookup_artefact_by_id(item, artefact_id)
    if root_id is not None:
        root = lookup_artefact_by_id(item, root_id)
        if artefact.root_artefact.id != root.id:
            abort(404)
    return item, artefact


def _get_artefact_or_404(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Load an artefact from either the nested or legacy route parameters."""
    if uuid is not None:
        return Artefact.query.filter_by(uuid=uuid).first_or_404()
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    return artefact


def _artefact_view_kwargs(artefact):
    """Return standard kwargs for redirecting to an artefact view."""
    return {
        'item_id': artefact.item.url_id,
        'artefact_id': artefact.url_slug,
    }


def _redirect_to_artefact_view(artefact):
    """Redirect to the standard artefact view."""
    return redirect(url_for(f'{ROUTENAME}.view', **_artefact_view_kwargs(artefact)))


def _check_download_restrictions(artefact):
    """Return a redirect response when download restrictions block access."""
    if not artefact.restrictions:
        return None

    if not current_user.can_bypass_all_restrictions(artefact.restrictions):
        categories = ', '.join(r.restriction_type.label for r in artefact.restrictions)
        flash(f'Download restricted: {categories}', 'danger')
        return _redirect_to_artefact_view(artefact)

    if not request.args.get('confirm_bypass'):
        flash('This artefact has download restrictions. Use the download override button to confirm.', 'warning')
        return _redirect_to_artefact_view(artefact)

    return None


def _collect_ancestor_file_restrictions(ef):
    """Return all ExtractedFileRestriction objects on any ancestor of ef.

    Walks up the parent_file chain.  If an archive is restricted, every file
    inside it is also effectively restricted.
    """
    restrictions = []
    current = ef.parent_file
    while current is not None:
        restrictions.extend(current.restrictions)
        current = current.parent_file
    return restrictions


def _collect_all_file_restrictions(ef):
    """Return all ExtractedFileRestriction objects on ef and every descendant.

    For non-archive files this is O(1).  For archives the child_files tree is
    walked recursively; SQLAlchemy loads each level on access via the backref.
    """
    restrictions = list(ef.restrictions)
    for child in ef.child_files:
        restrictions.extend(_collect_all_file_restrictions(child))
    return restrictions


def _check_file_download_restrictions(ef):
    """Return a redirect when file-level restrictions block an extracted-file download.

    Called after _check_download_restrictions() has cleared artefact-level
    restrictions.  Checks restrictions on ef itself, on any nested descendants
    (so downloading an archive is blocked if any contained file is restricted),
    and on any ancestor archive/directory (so a file inside a restricted archive
    is also blocked).
    """
    all_restrictions = (
        _collect_all_file_restrictions(ef) +
        _collect_ancestor_file_restrictions(ef)
    )
    if not all_restrictions:
        return None

    if not current_user.can_bypass_all_restrictions(all_restrictions):
        categories = ', '.join({r.restriction_type.label for r in all_restrictions})
        flash(f'File download restricted: {categories}', 'danger')
        return _redirect_to_artefact_view(ef.partition.artefact)

    if not request.args.get('confirm_bypass'):
        flash('This file has download restrictions. Use the download override to confirm.', 'warning')
        return _redirect_to_artefact_view(ef.partition.artefact)

    return None


def _check_artefact_file_restrictions(artefact):
    """Block artefact download when any extracted file within it has restrictions.

    Called after _check_download_restrictions() has cleared artefact-level
    restrictions.  Uses a single query over all partitions of this artefact.
    Because ExtractedFileRestriction has .restriction_type, the existing
    can_bypass_all_restrictions() method works on these objects directly.
    """
    file_restrictions = (
        ExtractedFileRestriction.query
        .join(ExtractedFile, ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(Partition.artefact_id == artefact.id)
        .all()
    )

    if not file_restrictions:
        return None

    if not current_user.can_bypass_all_restrictions(file_restrictions):
        categories = ', '.join({r.restriction_type.label for r in file_restrictions})
        flash(f'Download restricted (artefact contains restricted files): {categories}', 'danger')
        return _redirect_to_artefact_view(artefact)

    if not request.args.get('confirm_bypass'):
        flash('This artefact contains files with download restrictions. Use the download override to confirm.', 'warning')
        return _redirect_to_artefact_view(artefact)

    return None


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>')
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>', endpoint='view_nested')
@login_required
def view(item_id, artefact_id, root_id=None):
    """View an artefact and its partitions/files."""
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    return _render_artefact_view(artefact)


@blueprint.route('/artefacts/<string:uuid>')
@login_required
def view_legacy(uuid):
    """Legacy flat-URL compat shim — resolves and renders without redirect."""
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    return _render_artefact_view(artefact)


@blueprint.route('/artefacts/<string:uuid>/tree')
@login_required
def tree(uuid):
    """Processing tree view — shows the full artefact derivation tree with analysis status."""
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    root = artefact.root_artefact
    if root is not artefact:
        return redirect(url_for(f'{ROUTENAME}.tree', uuid=root.uuid))
    tree_data, has_active, status_counts, total_count = _build_processing_tree(root)
    return render_template(
        'artefacts/tree.html',
        artefact=root,
        tree=tree_data,
        has_active_analyses=has_active,
        status_counts=status_counts,
        total_count=total_count,
    )


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/viewer')
@login_required
def viewer(item_id, artefact_id):
    """Viewer page for converted outputs (images, text, etc.)."""
    _, artefact = _resolve_artefact(item_id, artefact_id)
    return _render_viewer(artefact)


@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/viewer')
@login_required
def viewer_nested(item_id, root_id, artefact_id):
    """Viewer page for converted outputs (nested artefact)."""
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    return _render_viewer(artefact)


_VALID_VIEWER_COLUMNS = [2, 3, 4, 6, 8]
# Responsive grid classes: phones get 2 cols, tablets get an intermediate
# count, desktops get the user's selection. Values must match the ones in
# viewer.html's custom CSS (.col-custom-N for non-12-divisible counts).
_COLUMN_CLASSES = {
    2: 'col-6',
    3: 'col-6 col-sm-4',
    4: 'col-6 col-sm-4 col-md-3',
    6: 'col-6 col-sm-4 col-md-3 col-lg-2',
    8: 'col-6 col-sm-4 col-md-3 col-lg-2 col-custom-8-xl',
}


def _render_viewer(artefact):
    """Build and render the viewer page for an artefact's converted outputs."""
    from collections import Counter, defaultdict
    from ..database import RestrictionType
    from ..utils.pagination import ListPagination, VALID_PER_PAGE, resolve_per_page

    _viewable_types = (ArtefactType.ACORN_SPRITE, ArtefactType.ACORN_DRAW, ArtefactType.ACORN_TEXT)
    output_groups = []

    def _enrich_outputs(outputs):
        """For text outputs, read file content for inline rendering."""
        storage = current_app.storage
        for out in outputs:
            if out.get('type') == 'text':
                try:
                    key = storage.storage_key('outputs', out['filename'])
                    with storage.open_read(key) as f:
                        out['text_content'] = f.read().decode('utf-8', errors='replace')
                except Exception:
                    out['text_content'] = None
        return outputs

    viewer_status = None  # 'pending', 'failed', 'partial', or None (ready)
    failed_conversion_list = []  # [{source_file, error, analysis_uuid}, ...]
    file_filter = request.args.get('file')
    # Subdirectory browse filter — matches the File Viewer's ?path=<dir>/ scheme
    # so selecting a subdirectory there carries through to the Viewer.
    current_path = request.args.get('path', '').strip()
    if current_path and not current_path.endswith('/'):
        current_path += '/'
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    use_pagination = False  # only paginate Mode 2 aggregate view without ?file=

    if artefact.artefact_type in _viewable_types:
        # Mode 1: Artefact is itself a viewable type — show its own FORMAT_CONVERT output
        conv = Analysis.query.filter_by(
            artefact_id=artefact.id,
            analysis_type=AnalysisType.FORMAT_CONVERT,
        ).order_by(Analysis.id.desc()).first()
        if conv and conv.status == AnalysisStatus.COMPLETED and conv.success:
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                details = {}
            outputs = _enrich_outputs(details.get('outputs', []))
            if outputs:
                output_groups.append({
                    'label': artefact.original_filename or artefact.label,
                    'source_file': None,
                    'outputs': outputs,
                })
        elif conv and conv.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING):
            viewer_status = 'pending'
        else:
            viewer_status = 'failed'
            if conv and conv.status == AnalysisStatus.FAILED:
                failed_conversion_list = [{
                    'source_file': artefact.original_filename or artefact.label,
                    'error': conv.error_message or 'Conversion failed',
                    'analysis_uuid': conv.uuid,
                }]
    else:
        # Mode 2: Aggregate outputs from all FORMAT_CONVERT analyses on this artefact
        # and all derived artefacts (e.g. an ISO extracted from a ZIP).
        convs = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.FORMAT_CONVERT,
            )
            .order_by(Analysis.id)
            .all()
        )

        pending_count = sum(
            1 for c in convs
            if c.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)
        )

        # Collect outputs grouped by source_file (filtered by ?file= if set)
        # and gather any per-file conversion failures recorded in details JSON.
        groups: dict[str, list] = defaultdict(list)
        for conv in convs:
            if conv.status == AnalysisStatus.FAILED:
                if not file_filter:
                    failed_conversion_list.append({
                        'source_file': None,
                        'error': conv.error_message or 'Conversion failed',
                        'analysis_uuid': conv.uuid,
                    })
                continue
            if not (conv.status == AnalysisStatus.COMPLETED and conv.success):
                continue
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            for fc in details.get('failed_conversions', []):
                if file_filter and fc.get('source_file') != file_filter:
                    continue
                failed_conversion_list.append({**fc, 'analysis_uuid': conv.uuid})
            for out in details.get('outputs', []):
                source = out.get('source_file', '')
                if file_filter and source != file_filter:
                    continue
                groups[source].append(out)

        for source_file, outputs in groups.items():
            # NOTE: do NOT call _enrich_outputs here — deferred to current page only
            label = source_file if source_file else (artefact.original_filename or artefact.label)
            output_groups.append({'label': label, 'source_file': source_file, 'outputs': outputs})

        # Sort groups alphabetically by label for stable ordering across pages
        output_groups.sort(key=lambda g: g['label'].lower())

        # Apply subdirectory filter (Mode 2 only).  Matches File Viewer ?path=
        # semantics: include everything under the prefix (current level + deeper).
        if current_path:
            output_groups = [
                g for g in output_groups
                if (g.get('source_file') or '').startswith(current_path)
            ]

        if not output_groups:
            viewer_status = 'pending' if pending_count > 0 else 'failed'
        elif pending_count > 0:
            viewer_status = 'partial'

        # Enable pagination for aggregate view without ?file= filter
        if not file_filter:
            use_pagination = True

    # ── Subdirectory navigation (Mode 2) ─────────────────────────────────────
    # Compute before the filetype filter so subdirectories stay visible even
    # when the current filetype filter empties the grid.
    subdirectories: list = []
    archive_paths: set = set()
    if not file_filter and artefact.artefact_type not in _viewable_types:
        from ..utils.path_nav import compute_subdirectories
        source_files_in_scope = [
            g.get('source_file') for g in output_groups if g.get('source_file')
        ]
        subdirectories = compute_subdirectories(source_files_in_scope, current_path)
        if subdirectories:
            partition_ids_arc = [
                p.id for p in Partition.query.filter(
                    Partition.artefact_id.in_(all_artefact_ids)
                ).all()
            ]
            if partition_ids_arc:
                archive_paths = {
                    row.path for row in (
                        ExtractedFile.query
                        .filter(
                            ExtractedFile.partition_id.in_(partition_ids_arc),
                            ExtractedFile.is_archive == True,
                        )
                        .with_entities(ExtractedFile.path)
                        .all()
                    )
                }

    # ── Filetype facet (Mode 2 only, when there are source_file paths) ───────
    filetype_facet = []  # [(hex_code, count), ...] sorted by count desc
    active_filetypes = set()
    filetype_toggle_urls = {}
    clear_filter_args = {}

    source_paths = [g['source_file'] for g in output_groups if g.get('source_file')]
    if source_paths:
        partition_ids = [
            p.id for p in Partition.query.filter(
                Partition.artefact_id.in_(all_artefact_ids)
            ).all()
        ]
        if partition_ids:
            filetype_rows = (
                ExtractedFile.query
                .filter(
                    ExtractedFile.partition_id.in_(partition_ids),
                    ExtractedFile.path.in_(source_paths),
                )
                .with_entities(ExtractedFile.path, ExtractedFile.risc_os_filetype)
                .all()
            )
            path_to_filetype = {r.path: r.risc_os_filetype for r in filetype_rows}

            # Tag each group with its filetype
            for group in output_groups:
                group['filetype'] = path_to_filetype.get(group.get('source_file'))

            # Build facet as a list of (hex_code, count) sorted by count desc,
            # then hex asc as tiebreaker. Template iterates this list directly.
            counts = Counter(
                g['filetype'] for g in output_groups if g.get('filetype')
            )
            filetype_facet = sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )

            # Apply filetype filter from ?filetype=ff9,fff
            filetype_param = request.args.get('filetype', '')
            active_filetypes = set(filetype_param.split(',')) - {''}
            if active_filetypes:
                output_groups = [
                    g for g in output_groups
                    if g.get('filetype') in active_filetypes
                ]

            # Build toggle URLs for the template
            base_args = {k: v for k, v in request.args.items()
                         if k not in ('filetype', 'page')}
            for ft, _ in filetype_facet:
                toggled = active_filetypes ^ {ft}
                args = dict(base_args)
                if toggled:
                    args['filetype'] = ','.join(sorted(toggled))
                filetype_toggle_urls[ft] = args
            clear_filter_args = dict(base_args)

    # ── Summary counts (post-filter, pre-pagination) ─────────────────────────
    total_counts = Counter()
    for g in output_groups:
        for out in g['outputs']:
            total_counts[out.get('type', 'unknown')] += 1
    total_groups = len(output_groups)

    # ── Pagination (Mode 2 aggregate only, without ?file=) ───────────────────
    # pagination_args is always populated so column/filter URLs preserve state
    # in non-paginated views (Mode 1 or ?file=) too.
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}
    pagination = None
    view_all = False

    if use_pagination and output_groups:
        per_page, page, view_all = resolve_per_page('VIEWER_PER_PAGE', 25)
        pagination = ListPagination(output_groups, page, per_page)
        # Only enrich text content for the current page
        for group in pagination.items:
            _enrich_outputs(group['outputs'])
        output_groups = pagination.items
    else:
        # Mode 1 or ?file= — enrich all (typically small set)
        for group in output_groups:
            _enrich_outputs(group['outputs'])

    # ── Configurable columns ─────────────────────────────────────────────────
    columns_param = request.args.get('columns', None, type=int)
    if columns_param in _VALID_VIEWER_COLUMNS:
        viewer_columns = columns_param
        if (current_user.is_authenticated
                and current_user.get_preference('viewer_columns') != columns_param):
            current_user.set_preference('viewer_columns', columns_param)
            db.session.commit()
    else:
        saved = None
        if current_user.is_authenticated:
            saved = current_user.get_preference('viewer_columns')
        viewer_columns = saved if saved in _VALID_VIEWER_COLUMNS else 4

    viewer_col_class = _COLUMN_CLASSES[viewer_columns]

    # ── Look up RISC OS module detail when ?file= matches a module path ──────
    module_detail = None
    if file_filter:
        mod_row = RiscosModule.query.filter(
            RiscosModule.artefact_id.in_(all_artefact_ids),
            RiscosModule.file_path == file_filter,
        ).first()
        if mod_row:
            module_detail = {
                'title_string': mod_row.title_string,
                'help_title': mod_row.help_title,
                'version': mod_row.version,
                'date': mod_row.date,
                'swi_chunk': mod_row.swi_chunk,
                'module_hash': mod_row.module_hash,
                'file_path': mod_row.file_path,
                'swi_names': None,
                'module_flags': None,
                'commands': [],
            }
            mod_analysis = Analysis.query.filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.RISCOS_MODULE_PARSE,
                Analysis.status == AnalysisStatus.COMPLETED,
            ).order_by(Analysis.id.desc()).first()
            if mod_analysis:
                try:
                    details = json.loads(mod_analysis.details or '{}')
                except (json.JSONDecodeError, TypeError):
                    details = {}
                for m in details.get('modules', []):
                    if m.get('file_path') == file_filter:
                        module_detail['swi_names'] = m.get('swi_names')
                        module_detail['module_flags'] = m.get('module_flags')
                        module_detail['commands'] = m.get('commands', [])
                        break

    # ── Explicit-content gate ────────────────────────────────────────────────
    explicit_type = RestrictionType.EXPLICIT
    user_can_bypass_explicit = current_user.can_bypass_restriction(explicit_type)

    artefact_is_explicit = any(
        r.restriction_type == explicit_type for r in artefact.restrictions
    )

    explicit_file_paths: set[str] = set()
    if not artefact_is_explicit:
        partition_ids_expl = [
            p.id for p in Partition.query.filter(
                Partition.artefact_id.in_(all_artefact_ids)
            ).all()
        ]
        if partition_ids_expl:
            explicit_efs = (
                ExtractedFile.query
                .join(ExtractedFileRestriction,
                      ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
                .filter(
                    ExtractedFileRestriction.restriction_type == explicit_type,
                    ExtractedFile.partition_id.in_(partition_ids_expl),
                )
                .with_entities(ExtractedFile.path)
                .all()
            )
            explicit_file_paths = {row.path for row in explicit_efs}

    for group in output_groups:
        group['explicit'] = (
            artefact_is_explicit or group.get('source_file') in explicit_file_paths
        )
        # Stable short ID for the explicit-gate sessionStorage key — must be
        # unique per content so consent doesn't bleed between different files
        # (especially across paginated pages, where loop.index collides).
        key_source = group.get('source_file') or group.get('label') or ''
        group['stable_id'] = hashlib.md5(
            f"{artefact.uuid}:{key_source}".encode('utf-8')
        ).hexdigest()[:12]

    return render_template(
        'artefacts/viewer.html',
        artefact=artefact,
        output_groups=output_groups,
        viewer_status=viewer_status,
        failed_conversion_list=failed_conversion_list,
        module_detail=module_detail,
        user_can_bypass_explicit=user_can_bypass_explicit,
        pagination=pagination,
        pagination_args=pagination_args,
        view_all=view_all,
        valid_per_page=VALID_PER_PAGE,
        total_counts=total_counts,
        total_groups=total_groups,
        filetype_facet=filetype_facet,
        active_filetypes=active_filetypes,
        filetype_toggle_urls=filetype_toggle_urls,
        clear_filter_args=clear_filter_args,
        viewer_columns=viewer_columns,
        viewer_col_class=viewer_col_class,
        valid_viewer_columns=_VALID_VIEWER_COLUMNS,
        current_path=current_path,
        subdirectories=subdirectories,
        archive_paths=archive_paths,
    )


def _render_artefact_view(artefact):

    # Only bind to request.args when the user has actively submitted a filter,
    # so that BooleanField defaults (e.g. recursive=True) apply on first load.
    # Without this, WTForms treats missing checkbox keys as False.
    _file_filter_keys = {'partition_uuid', 'filename', 'extension', 'path', 'md5', 'sha1',
                         'hide_known', 'filter_products', 'show_directories'}
    if _file_filter_keys & set(request.args.keys()):
        file_form = FileSearchForm(request.args)
    else:
        file_form = FileSearchForm()

    # Check if user wants to see all analyses or just the most recent N successful
    show_all_analyses = request.args.get('show_all_analyses', 'false').lower() == 'true'

    # Collect all artefact IDs: current + all derived (recursively).
    # Used for both partitions/files and analyses so that follow-on jobs
    # queued against derived partition artefacts are visible here.
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)

    # How many recent successful analyses to show in the default (non-show-all) view.
    # Configurable via ANALYSES_SHOWN in myapp.cfg (default: 5).
    analyses_shown_limit = current_app.config.get('ANALYSES_SHOWN', 5)

    # Fetch all related analyses for stats, newest first (eager-load artefact for template)
    from sqlalchemy.orm import joinedload as _jl_a
    all_related_analyses = Analysis.query.filter(
        Analysis.artefact_id.in_(all_artefact_ids)
    ).options(_jl_a(Analysis.artefact)).order_by(Analysis.id.desc()).all()
    total_analyses_count = len(all_related_analyses)

    # Status breakdown counts (displayed in the card header)
    status_counts = {s.value: 0 for s in AnalysisStatus}
    for a in all_related_analyses:
        status_counts[a.status.value] += 1

    if show_all_analyses:
        analyses = all_related_analyses  # already sorted newest first
    else:
        # Default view: always show active analyses (pending/running),
        # plus the N most recent completed (successful) analyses.
        # Failed and older analyses are hidden; click "Show All" to see them.
        active = [a for a in all_related_analyses
                  if a.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)][:analyses_shown_limit]
        completed = [a for a in all_related_analyses
                     if a.status == AnalysisStatus.COMPLETED][:analyses_shown_limit]
        seen_ids = {a.id for a in active + completed}
        analyses = [a for a in all_related_analyses if a.id in seen_ids]
        # all_related_analyses is already newest-first, so analyses inherits that order

    has_hidden_analyses = not show_all_analyses and total_analyses_count > len(analyses)

    # Query partitions from all artefacts (for display)
    all_partitions = Partition.query.filter(
        Partition.artefact_id.in_(all_artefact_ids)
    ).order_by(Partition.artefact_id, Partition.partition_index).all()

    from sqlalchemy.orm import selectinload as _sil
    files_query = ExtractedFile.query.join(Partition).filter(
        Partition.artefact_id.in_(all_artefact_ids)
    ).options(
        _sil(ExtractedFile.partition),
        _sil(ExtractedFile.known_file).selectinload(KnownFile.product),
        _sil(ExtractedFile.known_file).selectinload(KnownFile.database),
    )

    # Filter by specific partition if requested.
    # Guard against the string "None" which can arrive when Jinja2
    # renders a None value into a URL parameter.
    if file_form.partition_uuid.data in (None, '', 'None'):
        file_form.partition_uuid.data = None
    if file_form.partition_uuid.data:
        files_query = files_query.filter(Partition.uuid == file_form.partition_uuid.data)

    # Show empty directory entries so users can see the full disc structure

    if file_form.filename.data:
        fn = file_form.filename.data
        if '*' in fn or '?' in fn:
            # Glob pattern: escape SQL special chars then convert glob wildcards
            like_pat = fn.replace('%', r'\%').replace('_', r'\_').replace('*', '%').replace('?', '_')
            files_query = files_query.filter(ExtractedFile.filename.ilike(like_pat))
        else:
            # Plain text: substring match (backward-compatible behaviour)
            files_query = files_query.filter(ExtractedFile.filename.ilike(f'%{fn}%'))

    if file_form.filetype.data:
        # Strip a leading '#' or '&' that users might include with the hex value,
        # then resolve either a hex code or a name (e.g. "Drawfile") to a hex code.
        ft_raw = file_form.filetype.data.strip().lstrip('#&')
        ft = lookup_filetype_hex(ft_raw)
        if ft is None:
            # Not a known name or valid hex — no files can match
            from sqlalchemy import false as _false
            files_query = files_query.filter(_false())
        else:
            files_query = files_query.filter(ExtractedFile.risc_os_filetype == ft)

    if file_form.path.data:
        path_filter = file_form.path.data.strip()
        if path_filter.endswith('/'):
            # Directory browse: plain prefix match (existing behaviour)
            files_query = files_query.filter(
                ExtractedFile.path.ilike(f'{path_filter}%')
            )
        else:
            # Exact entry (e.g. from search result link) plus all contents
            from sqlalchemy import or_
            files_query = files_query.filter(
                or_(
                    ExtractedFile.path == path_filter,
                    ExtractedFile.path.ilike(f'{path_filter}/%')
                )
            )

    if file_form.md5.data:
        files_query = files_query.filter(ExtractedFile.md5 == file_form.md5.data.lower())
    
    if file_form.sha1.data:
        files_query = files_query.filter(ExtractedFile.sha1 == file_form.sha1.data.lower())
    
    if file_form.hide_known.data == 'hide':
        # Always show archive files even when hiding known files, because
        # archives serve as navigational pseudo-directories in the UI.
        from sqlalchemy import or_
        files_query = files_query.filter(
            or_(ExtractedFile.is_known == False, ExtractedFile.is_archive == True)
        )
    elif file_form.hide_known.data == 'only':
        from sqlalchemy import or_
        files_query = files_query.filter(
            or_(ExtractedFile.is_known == True, ExtractedFile.is_archive == True)
        )

    if file_form.filter_products.data == 'hide':
        # Hide files whose primary known_file match has a product association.
        from sqlalchemy import or_ as _or
        files_query = files_query.filter(
            _or(
                ExtractedFile.known_file_id == None,
                ~ExtractedFile.known_file.has(KnownFile.product_id != None),
                ExtractedFile.is_archive == True,
            )
        )
    elif file_form.filter_products.data == 'only':
        from sqlalchemy import or_ as _or
        files_query = files_query.filter(
            _or(
                ExtractedFile.known_file.has(KnownFile.product_id != None),
                ExtractedFile.is_archive == True,
            )
        )
    
    from ..utils.pagination import resolve_per_page, VALID_PER_PAGE
    per_page, page, view_all = resolve_per_page('FILES_PER_PAGE', 100)

    # Column sorting: sort=<col> ascending, sort=-<col> descending
    sort_param = request.args.get('sort', 'path')
    sort_desc = sort_param.startswith('-')
    sort_col = sort_param.lstrip('-')
    from sqlalchemy import desc, func as _func
    _sort_columns = {
        'path': _func.lower(ExtractedFile.path),
        'size': ExtractedFile.file_size,
        'filetype': ExtractedFile.risc_os_filetype,
        'known': ExtractedFile.is_known,
        'date': ExtractedFile.modified_time,
    }
    sort_expr = _sort_columns.get(sort_col, _func.lower(ExtractedFile.path))
    if sort_desc:
        sort_expr = desc(sort_expr)

    # Compute letter-to-page mapping for A-Z jump bar (only for path sort)
    if sort_col == 'path':
        from ..utils.pagination import compute_letter_pages
        letter_pages, current_letter = compute_letter_pages(
            files_query, ExtractedFile.path,
            per_page, current_page=page, descending=sort_desc
        )
    else:
        letter_pages, current_letter = {}, ''

    files_pagination = files_query.order_by(sort_expr).paginate(
        page=page, per_page=per_page, max_per_page=per_page
    )

    # Batch-query all matching KnownFiles across active hash databases
    # for the current page of files, so the template can show multiple badges.
    from ..utils.hash_rescan import find_all_known_files_batch
    from ..database import RestrictionType
    file_known_matches = find_all_known_files_batch(files_pagination.items)

    # Build query args for pagination links, preserving all active filters
    pagination_args = request.args.to_dict()
    pagination_args.pop('page', None)
    # Keep 'mode' in pagination_args so pagination/sort/per-page links
    # preserve hashdb mode.  The toggle button uses hashdb_toggle_args
    # (without mode) so it can toggle freely.
    hashdb_toggle_args = {k: v for k, v in pagination_args.items() if k != 'mode'}
    current_sort = sort_param

    # Extract subdirectories at the current path level for directory browsing
    current_path = file_form.path.data.strip() if file_form.path.data else ''
    subdirectories: list = []

    if all_partitions:
        from ..utils.path_nav import compute_subdirectories

        # Infer subdirectories from file paths (covers non-empty directories).
        all_file_paths = [p for (p,) in files_query.with_entities(ExtractedFile.path).all()]
        subdir_set = set(compute_subdirectories(all_file_paths, current_path))

        # Also surface explicit is_directory=True entries (covers empty directories
        # recorded by the worker). These are excluded from files_query when
        # filters suppress directory rows, so query them separately.
        dir_entries_query = (
            ExtractedFile.query.join(Partition)
            .filter(
                Partition.artefact_id.in_(all_artefact_ids),
                ExtractedFile.is_directory == True,
            )
            .with_entities(ExtractedFile.path)
        )
        if file_form.partition_uuid.data:
            dir_entries_query = dir_entries_query.filter(
                Partition.uuid == file_form.partition_uuid.data
            )
        for (dir_path,) in dir_entries_query.all():
            if current_path:
                if not dir_path.startswith(current_path):
                    continue
                relative_path = dir_path[len(current_path):]
            else:
                relative_path = dir_path
            # Only add direct children (no slash = not a deeper descendant)
            if relative_path and '/' not in relative_path:
                subdir_set.add(relative_path)

        from natsort import natsorted, ns
        subdirectories = natsorted(subdir_set, alg=ns.IGNORECASE)

    # Build a set of archive file paths so the template can show archive
    # icons for "directories" that are actually archives.
    archive_paths = set()
    if all_partitions:
        archive_files = ExtractedFile.query.join(Partition).filter(
            Partition.artefact_id.in_(all_artefact_ids),
            ExtractedFile.is_archive == True
        ).with_entities(ExtractedFile.path).all()
        archive_paths = {af.path for af in archive_files}

    # Extract completed analysis results for display.
    # These are surfaced as badges + cards directly on the artefact view page.
    mastering_analysis = None
    protection_analysis = None
    partition_detect_details = None
    armlock_analysis = None
    flux_visualisation_analysis = None
    for a in all_related_analyses:
        if a.status == AnalysisStatus.COMPLETED and a.details:
            if mastering_analysis is None and a.analysis_type == AnalysisType.DISC_MASTERING_DETECT:
                try:
                    mastering_analysis = json.loads(a.details)
                    mastering_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse mastering analysis details for {a.uuid}: {e}")
            elif protection_analysis is None and a.analysis_type == AnalysisType.DISC_PROTECTION_DETECT:
                try:
                    protection_analysis = json.loads(a.details)
                    protection_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse protection analysis details for {a.uuid}: {e}")
            elif partition_detect_details is None and a.analysis_type == AnalysisType.PARTITION_DETECT:
                try:
                    partition_detect_details = json.loads(a.details)
                    partition_detect_details['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse partition detect details for {a.uuid}: {e}")
            elif armlock_analysis is None and a.analysis_type == AnalysisType.ARMLOCK_REMOVE:
                try:
                    armlock_analysis = json.loads(a.details)
                    armlock_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse ARMlock analysis details for {a.uuid}: {e}")
            elif flux_visualisation_analysis is None and a.analysis_type == AnalysisType.FLUX_VISUALISATION:
                try:
                    flux_visualisation_analysis = json.loads(a.details)
                    flux_visualisation_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse flux visualisation analysis details for {a.uuid}: {e}")
        if (mastering_analysis is not None and protection_analysis is not None
                and partition_detect_details is not None
                and flux_visualisation_analysis is not None):
            break

    # Build a lookup of per-partition metadata from PARTITION_DETECT, keyed by
    # partition index, so the template can display disc names, passwords,
    # protection levels, and flags inline in the Partitions table.
    partition_metadata = {}
    if partition_detect_details:
        for p in partition_detect_details.get('partitions', []):
            idx = p.get('index')
            if idx is not None:
                partition_metadata[idx] = p

    hashdb_mode = request.args.get('mode') == 'hashdb'

    # Build viewable_filenames and failed_conversion_info by scanning all
    # FORMAT_CONVERT analyses for this artefact tree.
    #   viewable_filenames    — set of file.path strings with completed outputs
    #   failed_conversion_info — dict file.path → {error, analysis_uuid, kind}
    #     kind='conversion'  for per-file FORMAT_CONVERT failures
    #     kind='extraction'  for ARCHIVE_EXTRACT failures (see below)
    _viewable_types = (ArtefactType.ACORN_SPRITE, ArtefactType.ACORN_DRAW, ArtefactType.ACORN_TEXT)
    viewable_filenames = set()
    failed_conversion_info = {}
    if artefact.artefact_type not in _viewable_types:
        convs = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.FORMAT_CONVERT,
            )
            .all()
        )
        for conv in convs:
            if not (conv.status == AnalysisStatus.COMPLETED and conv.success):
                continue
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            for out in details.get('outputs', []):
                sf = out.get('source_file')
                if sf:
                    viewable_filenames.add(sf)
            for fc in details.get('failed_conversions', []):
                sf = fc.get('source_file')
                if sf and sf not in failed_conversion_info:
                    failed_conversion_info[sf] = {
                        'error': fc.get('error', 'Conversion failed'),
                        'analysis_uuid': conv.uuid,
                        'kind': 'conversion',
                    }

        # Collect ARCHIVE_EXTRACT failures and map back to the archive file's
        # path via the file_id stored in hints.
        failed_extractions = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.ARCHIVE_EXTRACT,
                Analysis.status == AnalysisStatus.FAILED,
            )
            .all()
        )
        if failed_extractions:
            failed_file_ids = {}
            for ae in failed_extractions:
                try:
                    hints = json.loads(ae.hints or '{}')
                except (json.JSONDecodeError, TypeError):
                    hints = {}
                file_id = hints.get('file_id')
                if file_id:
                    failed_file_ids[int(file_id)] = {
                        'error': ae.error_message or 'Extraction failed',
                        'analysis_uuid': ae.uuid,
                        'kind': 'extraction',
                    }
            if failed_file_ids:
                efs = ExtractedFile.query.filter(
                    ExtractedFile.id.in_(failed_file_ids.keys())
                ).all()
                for ef in efs:
                    failed_conversion_info[ef.path] = failed_file_ids[ef.id]

    # Build module_info: dict mapping ExtractedFile.path → module metadata for
    # files with filetype ffa.  Used by the file listing template to show a
    # tooltip with the module's internal name, version, and date.
    # Query across all derived artefacts so modules found in e.g. an ISO
    # extracted from a ZIP are visible when viewing the parent ZIP.
    module_info = {}
    all_modules = RiscosModule.query.filter(
        RiscosModule.artefact_id.in_(all_artefact_ids)
    ).all()
    for mod in all_modules:
        if mod.file_path:
            module_info[mod.file_path] = mod

    # "View" button: show if artefact is viewable type, or has any FORMAT_CONVERT
    has_converted_outputs = artefact.artefact_type in _viewable_types
    if not has_converted_outputs:
        has_converted_outputs = Analysis.query.filter(
            Analysis.artefact_id.in_(all_artefact_ids),
            Analysis.analysis_type == AnalysisType.FORMAT_CONVERT,
        ).first() is not None

    # Recognised products for all partitions of this artefact tree
    recognised_products = []
    if all_partitions:
        partition_ids = [p.id for p in all_partitions]
        from sqlalchemy.orm import joinedload as _jl
        recognised_products = (
            RecognisedProduct.query
            .join(RecognisedProduct.partition)
            .filter(RecognisedProduct.partition_id.in_(partition_ids))
            .options(_jl(RecognisedProduct.product).joinedload(KnownProduct.database))
            .order_by(Partition.partition_index, RecognisedProduct.folder_path)
            .all()
        )

    # Build a set of folder paths that have a recognised product (for directory row badges)
    recognised_folder_paths = {rp.folder_path: rp for rp in recognised_products}

    # Hash databases for the "Add to Hash DB" modal (with products pre-loaded)
    if hashdb_mode:
        from sqlalchemy.orm import joinedload as _jl2
        hash_databases = (
            HashDatabase.query
            .options(_jl2(HashDatabase.known_products))
            .order_by(HashDatabase.name)
            .all()
        )
    else:
        hash_databases = []

    # File-level restrictions on any extracted file within this artefact tree.
    # Used to adjust the download button state when the artefact itself is
    # unrestricted but contains restricted extracted files.
    from sqlalchemy.orm import joinedload as _jl_efr
    artefact_file_restrictions = (
        ExtractedFileRestriction.query
        .options(_jl_efr(ExtractedFileRestriction.extracted_file))
        .join(ExtractedFile, ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(Partition.artefact_id.in_(all_artefact_ids))
        .all()
    )

    # Build two mappings for non-direct restriction display in the file listing:
    #
    #   file_ancestor_restrictions {file_id: [restrictions]}
    #     A file inside a restricted archive — the restriction comes from above.
    #
    #   file_descendant_restrictions {file_id: [restrictions]}
    #     An archive whose contents include a restricted file — the restriction
    #     originates from below.
    #
    # Strategy: one query for the parent_id map of all files in the artefact
    # tree, then two in-memory passes.
    file_ancestor_restrictions: dict[int, list] = {}
    file_descendant_restrictions: dict[int, list] = {}
    if artefact_file_restrictions:
        # direct map: file_id -> [restriction objects]
        _direct_map: dict[int, list] = {}
        for r in artefact_file_restrictions:
            _direct_map.setdefault(r.extracted_file_id, []).append(r)

        # parent map: file_id -> parent_file_id (None for top-level)
        _parent_rows = (
            ExtractedFile.query
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .filter(Partition.artefact_id.in_(all_artefact_ids))
            .with_entities(ExtractedFile.id, ExtractedFile.parent_file_id)
            .all()
        )
        _parent_map: dict[int, int | None] = {row.id: row.parent_file_id for row in _parent_rows}

        # Pass 1 — upward: for every directly restricted file, mark all of
        # its ancestor archives as having a restriction originating from below.
        for restricted_id, restr_list in _direct_map.items():
            pid = _parent_map.get(restricted_id)
            while pid is not None:
                file_descendant_restrictions.setdefault(pid, []).extend(restr_list)
                pid = _parent_map.get(pid)

        # Pass 2 — downward (current page only): for files on this page that
        # have no direct restrictions, check whether any enclosing archive is
        # restricted and propagate that restriction down to them.
        for f in files_pagination.items:
            if f.id in _direct_map:
                continue  # has direct restrictions — handled by file.restrictions in template
            inherited = []
            pid = _parent_map.get(f.id)
            while pid is not None:
                if pid in _direct_map:
                    inherited.extend(_direct_map[pid])
                pid = _parent_map.get(pid)
            if inherited:
                file_ancestor_restrictions.setdefault(f.id, []).extend(inherited)

    def _dedup_by_type(rlist):
        """Return rlist with duplicate restriction_type entries removed (keeps first)."""
        seen: set = set()
        result = []
        for r in rlist:
            if r.restriction_type not in seen:
                seen.add(r.restriction_type)
                result.append(r)
        return result

    # Deduplicate each per-file list so that e.g. an archive containing five
    # MALWARE-restricted files doesn't show the badge five times.
    file_ancestor_restrictions = {
        fid: _dedup_by_type(rlist)
        for fid, rlist in file_ancestor_restrictions.items()
    }
    file_descendant_restrictions = {
        fid: _dedup_by_type(rlist)
        for fid, rlist in file_descendant_restrictions.items()
    }

    # Legacy alias used by the download-button logic in the template — the
    # effective non-direct restrictions are the union of both directions.
    file_inherited_restrictions = {
        fid: _dedup_by_type(
            file_ancestor_restrictions.get(fid, []) + file_descendant_restrictions.get(fid, [])
        )
        for fid in set(file_ancestor_restrictions) | set(file_descendant_restrictions)
    }

    return render_template('artefacts/view.html',
                           artefact=artefact,
                           analyses=analyses,
                           show_all_analyses=show_all_analyses,
                           has_hidden_analyses=has_hidden_analyses,
                           total_analyses_count=total_analyses_count,
                           status_counts=status_counts,
                           file_form=file_form,
                           files=files_pagination.items,
                           files_pagination=files_pagination,
                           pagination_args=pagination_args,
                           hashdb_toggle_args=hashdb_toggle_args,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all,
                           all_partitions=all_partitions,
                           subdirectories=subdirectories,
                           current_path=current_path,
                           archive_paths=archive_paths,
                           current_sort=current_sort,
                           mastering_analysis=mastering_analysis,
                           protection_analysis=protection_analysis,
                           armlock_analysis=armlock_analysis,
                           flux_visualisation_analysis=flux_visualisation_analysis,
                           partition_detect_details=partition_detect_details,
                           partition_metadata=partition_metadata,
                           hashdb_mode=hashdb_mode,
                           recognised_products=recognised_products,
                           recognised_folder_paths=recognised_folder_paths,
                           hash_databases=hash_databases,
                           file_known_matches=file_known_matches,
                           RestrictionType=RestrictionType,
                           letter_pages=letter_pages,
                           current_letter=current_letter,
                           viewable_filenames=viewable_filenames,
                           failed_conversion_info=failed_conversion_info,
                           has_converted_outputs=has_converted_outputs,
                           module_info=module_info,
                           artefact_file_restrictions=artefact_file_restrictions,
                           file_inherited_restrictions=file_inherited_restrictions,
                           file_ancestor_restrictions=file_ancestor_restrictions,
                           file_descendant_restrictions=file_descendant_restrictions,
                           move_item_choices=_move_item_choices(artefact))


def _move_item_choices(artefact):
    """Build item selector choices for the move-artefact form.

    Returns an empty list when the artefact is derived (cannot be moved).
    Items are shown with depth-based indentation to reflect hierarchy.
    """
    if artefact.parent_artefact_id is not None:
        return []

    from ..utils.item_helpers import indented_item_choices
    return indented_item_choices(
        value_fn=lambda item: item.url_id,
        exclude_ids={artefact.item_id},
    )


@blueprint.route('/<string:uuid>/add-to-hashdb', methods=['POST'])
@login_required
@require_permission('read_write')
def add_to_hashdb(uuid):
    """Add selected extracted files to a hash database."""
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()

    file_ids = request.form.getlist('file_ids', type=int)
    raw_db_id = request.form.get('database_id', '').strip()
    product_id = request.form.get('product_id', type=int)
    new_product_title = request.form.get('new_product_title', '').strip()
    new_product_description = request.form.get('new_product_description', '').strip()
    is_required = request.form.get('is_required', '1') == '1'
    base_path = request.form.get('base_path', '').strip()

    # Preserve directory navigation state across the redirect
    nav_partition_uuid = request.form.get('partition_uuid', '').strip() or None
    nav_path = request.form.get('nav_path', '').strip() or None
    redirect_kwargs = dict(item_id=artefact.item.url_id, artefact_id=artefact.url_slug, mode='hashdb')
    if nav_partition_uuid:
        redirect_kwargs['partition_uuid'] = nav_partition_uuid
    if nav_path:
        redirect_kwargs['path'] = nav_path

    if not file_ids:
        flash('No files selected.', 'warning')
        return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))

    if raw_db_id == 'new':
        new_db_name = request.form.get('new_database_name', '').strip()
        if not new_db_name:
            flash('Provide a name for the new hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))
        database = HashDatabase(name=new_db_name)
        db.session.add(database)
        db.session.flush()
    else:
        try:
            database_id = int(raw_db_id)
        except (ValueError, TypeError):
            flash('Select a hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))
        database = HashDatabase.query.get_or_404(database_id)

    # Create or fetch the product
    if product_id:
        product = KnownProduct.query.filter_by(id=product_id, database_id=database.id).first_or_404()
    elif new_product_title:
        product = KnownProduct(
            database_id=database.id,
            title=new_product_title,
            description=new_product_description or None,
        )
        db.session.add(product)
        db.session.flush()  # get product.id
    else:
        flash('Select a product or provide a new product title.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))

    # Get OUTPUT_FOLDER for on-demand hash computation
    output_folder = current_app.config.get('OUTPUT_FOLDER', '')
    if not os.path.isabs(output_folder):
        output_folder = os.path.join(current_app.instance_path, output_folder)

    added = 0
    new_kfs = []
    skipped_no_hash = []
    skipped_no_file = []

    for file_id in file_ids:
        ef = ExtractedFile.query.get(file_id)
        if ef is None or ef.partition.artefact_id not in _get_all_artefact_ids(artefact):
            continue
        if ef.is_directory:
            continue

        md5 = ef.md5
        sha1 = ef.sha1
        sha256 = ef.sha256

        # Compute hashes on demand if missing
        if not md5:
            file_path_on_disk = _resolve_extracted_file_path(ef)
            if not file_path_on_disk:
                skipped_no_file.append(ef.path)
                continue
            try:
                md5_h = hashlib.md5()
                sha1_h = hashlib.sha1()
                sha256_h = hashlib.sha256()
                with open(file_path_on_disk, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        md5_h.update(chunk)
                        sha1_h.update(chunk)
                        sha256_h.update(chunk)
                md5 = md5_h.hexdigest()
                sha1 = sha1_h.hexdigest()
                sha256 = sha256_h.hexdigest()
                # Persist back to ExtractedFile
                ef.md5 = md5
                ef.sha1 = sha1
                ef.sha256 = sha256
            except OSError:
                skipped_no_file.append(ef.path)
                continue

        # Deduplicate: skip if this md5 already exists in this product
        if KnownFile.query.filter_by(database_id=database.id, product_id=product.id, md5=md5).first():
            continue

        kf = KnownFile(
            database_id=database.id,
            product_id=product.id,
            filename=ef.filename,
            file_size=ef.file_size,
            md5=md5,
            sha1=sha1,
            sha256=sha256,
            is_required=is_required,
            relative_path=(ef.path[len(base_path):] if base_path and ef.path and ef.path.startswith(base_path) else ef.path) or None,
        )
        db.session.add(kf)
        new_kfs.append(kf)
        added += 1

    database.file_count = (database.file_count or 0) + added
    db.session.commit()

    # Trigger hash rescan and product recognition for the newly added files,
    # matching the behaviour of the per-file add_known_file route in hashdb.py.
    if new_kfs and database.is_active:
        from sqlalchemy import or_ as _or
        from ..utils.hash_rescan import rescan_hashes_for_new_known_files, queue_product_recognition_for_partitions
        rescan_hashes_for_new_known_files(new_kfs)
        if database.enable_product_recognition:
            conditions = []
            for kf in new_kfs:
                if kf.md5:
                    conditions.append(ExtractedFile.md5 == kf.md5)
                if kf.sha1:
                    conditions.append(ExtractedFile.sha1 == kf.sha1)
            if conditions:
                partition_ids = {
                    row[0] for row in
                    ExtractedFile.query
                    .with_entities(ExtractedFile.partition_id)
                    .filter(_or(*conditions))
                    .all()
                }
                if partition_ids:
                    queue_product_recognition_for_partitions(partition_ids)

    if added:
        flash(f'Added {added} file(s) to "{product.title}" in "{database.name}".', 'success')
    if skipped_no_hash:
        flash(f'{len(skipped_no_hash)} file(s) skipped — no hash available and extraction analysis not found. Re-run FILE_EXTRACTION first.', 'warning')
    if skipped_no_file:
        flash(f'{len(skipped_no_file)} file(s) skipped — extracted files no longer on disk.', 'warning')
    if not added and not skipped_no_hash and not skipped_no_file:
        flash('All selected files already exist in this hash database.', 'info')

    return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))


def _get_all_artefact_ids(artefact):
    """Return the set of IDs of the artefact and all derived artefacts (recursively)."""
    ids = {artefact.id}
    for derived in artefact.derived_artefacts:
        ids |= _get_all_artefact_ids(derived)
    return ids


@blueprint.route('/items/<string:item_id>/artefacts/upload', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def upload(item_id):
    """Upload a new artefact."""
    item = lookup_by_identifier(Item, item_id)
    form = ArtefactUploadForm()

    # Build type choices with auto-detect as default
    type_choices = [('auto', '-- Auto-detect --')]
    type_choices.extend([(t.value, t.value.upper().replace('_', ' ')) for t in ArtefactType if t != ArtefactType.UNKNOWN])
    form.artefact_type.choices = type_choices

    # Build platform choices
    platforms = Platform.query.order_by(Platform.name).all()
    form.platform_id.choices = [(0, '-- No hint --')] + [(p.id, p.name) for p in platforms]

    if form.validate_on_submit():
        file = form.file.data
        original_filename = safe_original_filename(file.filename)
        
        # Detect or use specified type
        if form.artefact_type.data == 'auto':
            artefact_type = detect_artefact_type(original_filename)
            type_overridden = False
        else:
            artefact_type = ArtefactType(form.artefact_type.data)
            type_overridden = True
        
        # Save file
        storage_path, file_size = save_uploaded_file(file)

        # Compute hashes and check for duplicates
        storage_key = current_app.storage.storage_key('uploads', storage_path)
        try:
            md5, sha256 = compute_file_hashes(storage_key, use_storage=True)
        except IOError:
            md5, sha256 = None, None

        if sha256:
            existing = Artefact.query.filter_by(item_id=item.id, sha256=sha256).first()
            if existing:
                try:
                    current_app.storage.delete(storage_key)
                except Exception:
                    pass
                flash(f'Duplicate: a file with identical content already exists as "{existing.label}".', 'warning')
                return redirect(url_for(f'{ROUTENAME}.upload', item_id=item.url_id))

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(original_filename)

        # Create artefact record
        artefact = Artefact(
            item_id=item.id,
            label=form.label.data,
            artefact_type=artefact_type,
            type_overridden=type_overridden,
            description=form.description.data,
            original_filename=original_filename,
            storage_path=storage_path,
            file_size=file_size,
            mime_type=mime_type,
            md5=md5,
            sha256=sha256,
        )
        
        db.session.add(artefact)
        db.session.commit()

        # Generate slug (unique within this item)
        base_slug = generate_slug(artefact.label)
        artefact.slug = ensure_unique_slug(base_slug, Artefact, scope_filter={'item_id': item.id})
        db.session.commit()

        # Always queue checksum computation via the worker; also queue type-specific
        # analyses if the user requested auto-analyse.
        hints = {}
        if form.platform_id.data and form.platform_id.data != 0:
            platform = Platform.query.get(form.platform_id.data)
            if platform:
                hints['platform'] = platform.name
        if form.dfi_clock_mhz.data:
            hints['dfi_clock_mhz'] = form.dfi_clock_mhz.data
        queue_analyses_for_artefact(artefact, hints if hints else None, checksum_only=not form.auto_analyse.data)
        if form.auto_analyse.data:
            flash(f'Artefact "{artefact.label}" uploaded. Analysis queued.', 'success')
        else:
            flash(f'Artefact "{artefact.label}" uploaded.', 'success')

        if form.upload_more.data:
            return redirect(url_for(f'{ROUTENAME}.upload', item_id=item.url_id, upload_more=1))
        return redirect(url_for(f'{ROUTENAME}.view', item_id=item.url_id, artefact_id=artefact.url_slug))

    if request.args.get('upload_more') == '1':
        form.upload_more.data = True
    return render_template('artefacts/upload.html', form=form, item=item)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/edit', methods=['GET', 'POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/edit', methods=['GET', 'POST'], endpoint='edit_nested')
@blueprint.route('/artefacts/<string:uuid>/edit', methods=['GET', 'POST'], endpoint='edit_legacy')
@login_required
@require_permission('read_write')
def edit(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Edit artefact metadata."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    form = ArtefactEditForm(obj=artefact)
    
    # Build type choices
    type_choices = [(t.value, t.value.upper().replace('_', ' ')) for t in ArtefactType]
    form.artefact_type.choices = type_choices
    
    if request.method == 'GET':
        form.artefact_type.data = artefact.artefact_type.value
        form.tags.data = ', '.join(t.name for t in artefact.tags)

    if form.validate_on_submit():
        artefact.label = form.label.data
        new_type = ArtefactType(form.artefact_type.data)
        if new_type != artefact.artefact_type:
            artefact.artefact_type = new_type
            artefact.type_overridden = True
        artefact.description = form.description.data

        artefact.tags.clear()
        if form.tags.data:
            tag_names = [t.strip() for t in form.tags.data.split(',') if t.strip()]
            existing = {t.name: t for t in Tag.query.filter(Tag.name.in_(tag_names)).all()}
            for tag_name in tag_names:
                tag = existing.get(tag_name)
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                artefact.tags.append(tag)

        db.session.commit()

        flash(f'Artefact "{artefact.label}" updated.', 'success')
        return _redirect_to_artefact_view(artefact)

    return render_template('artefacts/edit.html',
                           form=form,
                           artefact=artefact,
                           item=artefact.item)


def _delete_artefact_files(artefact):
    """Recursively delete files for an artefact and all its derived artefacts."""
    storage = current_app.storage
    for derived in artefact.derived_artefacts:
        _delete_artefact_files(derived)
    try:
        key = get_artefact_storage_key(artefact)
        storage.delete(key)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete file for artefact {artefact.uuid}: {e}")


def _cleanup_artefact_outputs_s3(artefact, storage):
    """Clean up analysis outputs for an artefact using S3 storage.

    Deletes output files, output directories (extraction trees), and
    cached partition images via the S3 storage backend.
    """
    cleanup = _collect_cleanup_paths_for_artefact(artefact)
    for filename in cleanup['output_files']:
        try:
            key = storage.storage_key('outputs', filename)
            storage.delete(key)
            current_app.logger.info(f"Deleted output file: {filename}")
        except Exception as e:
            current_app.logger.warning(f"Failed to delete output file {filename}: {e}")
    for path in cleanup['output_dirs']:
        try:
            if os.path.isabs(path):
                parts = path.rstrip('/').split('/')
                try:
                    idx = parts.index('outputs')
                    rel = '/'.join(parts[idx + 1:])
                except ValueError:
                    rel = '/'.join(parts[-3:]) if len(parts) >= 3 else parts[-1]
            else:
                rel = path
            prefix = storage.storage_key('outputs', rel)
            storage.delete_prefix(prefix)
        except Exception as e:
            current_app.logger.warning(f"Failed to delete output directory {path}: {e}")
    try:
        cache_prefix = storage.storage_key('outputs', f'.cache/{artefact.uuid}')
        storage.delete_prefix(cache_prefix)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete partition cache for artefact {artefact.uuid}: {e}")


def _delete_item_files(item):
    """Delete all files associated with an item's artefacts before DB cascade delete.

    For each artefact, removes the stored file (recursing into derived artefacts),
    analysis output directories, named output files, and cached partition images.
    Must be called while the ORM relationships are still intact (before db.session.delete).
    """
    storage = current_app.storage
    from shared.storage import S3Storage

    for artefact in item.artefacts:
        # Delete stored files for this artefact and all its derived artefacts.
        _delete_artefact_files(artefact)
        if isinstance(storage, S3Storage):
            _cleanup_artefact_outputs_s3(artefact, storage)
        else:
            _cleanup_artefact_outputs(artefact, current_app.logger)


def move_artefact_to_item(artefact, new_item):
    """Move a root artefact (and all its derived artefacts) to a different item.

    Only root artefacts (parent_artefact_id is None) may be moved.
    Slug uniqueness is re-checked in the target item; slugs are regenerated on
    collision.  This is a pure DB operation — no files move on disk.
    """
    if artefact.parent_artefact_id is not None:
        raise ValueError('Only root artefacts can be moved')
    if artefact.item_id == new_item.id:
        raise ValueError('Artefact is already in this item')

    def _update_item_id(art, target_item_id):
        art.item_id = target_item_id
        # Ensure slug is unique within the target item
        art.slug = ensure_unique_slug(
            art.slug, Artefact, existing_id=art.id,
            scope_filter={'item_id': target_item_id},
        )
        for derived in art.derived_artefacts:
            _update_item_id(derived, target_item_id)

    _update_item_id(artefact, new_item.id)
    db.session.commit()


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/move', methods=['POST'])
@login_required
@require_permission('read_write')
def move(item_id=None, artefact_id=None):
    """Move a root artefact to a different item."""
    artefact = _get_artefact_or_404(item_id, artefact_id)

    if artefact.parent_artefact_id is not None:
        flash('Only root artefacts can be moved.', 'danger')
        return _redirect_to_artefact_view(artefact)

    target_uuid = request.form.get('target_item_uuid')
    if not target_uuid:
        flash('No target item selected.', 'danger')
        return _redirect_to_artefact_view(artefact)

    target_item = lookup_by_identifier(Item, target_uuid)

    if target_item.id == artefact.item_id:
        flash('Artefact is already in that item.', 'warning')
        return _redirect_to_artefact_view(artefact)

    old_item_name = artefact.item.name
    move_artefact_to_item(artefact, target_item)

    flash(f'Artefact "{artefact.label}" moved from "{old_item_name}" to "{target_item.name}".', 'success')
    return _redirect_to_artefact_view(artefact)


def _collect_item_artefact_ids(item_ids):
    """Collect all artefact IDs for a list of items (direct + all derived) via CTE."""
    from sqlalchemy import select as sa_select

    direct_ids = [r[0] for r in db.session.execute(
        sa_select(Artefact.id).where(Artefact.item_id.in_(item_ids))
    ).all()]
    if not direct_ids:
        return []

    # Recursive CTE: find all derived artefacts from any artefact in this item
    base = sa_select(Artefact.id).where(Artefact.parent_artefact_id.in_(direct_ids))
    cte = base.cte(name='item_derived', recursive=True)
    recursive = sa_select(Artefact.id).where(Artefact.parent_artefact_id == cte.c.id)
    cte = cte.union_all(recursive)
    derived_ids = [r[0] for r in db.session.execute(sa_select(cte.c.id)).all()]

    return direct_ids + derived_ids


def _collect_item_cleanup_keys(all_artefact_ids):
    """Collect all storage keys and cache prefixes for cleanup.

    Returns a dict with keys: artefact_keys, output_file_keys, output_dir_prefixes,
    cache_prefixes.  All values are storage key strings usable with the storage backend.
    """
    from sqlalchemy import select as sa_select

    artefact_keys = []
    cache_prefixes = []
    output_dir_prefixes = []
    output_file_keys = []

    if not all_artefact_ids:
        return {
            'artefact_keys': artefact_keys,
            'output_file_keys': output_file_keys,
            'output_dir_prefixes': output_dir_prefixes,
            'cache_prefixes': cache_prefixes,
        }

    # Single query for artefact storage keys and cache prefixes
    rows = db.session.execute(
        sa_select(Artefact.storage_directory, Artefact.storage_path, Artefact.uuid)
        .where(Artefact.id.in_(all_artefact_ids))
    ).all()
    for storage_dir, storage_path, artefact_uuid in rows:
        if storage_path:
            directory = 'outputs' if storage_dir == StorageDirectory.OUTPUTS else 'uploads'
            artefact_keys.append(f"{directory}/{storage_path}")
        cache_prefixes.append(f"outputs/.cache/{artefact_uuid}")

    # Analysis output dirs and named output files
    rows = db.session.execute(
        sa_select(Analysis.output_path, Analysis.details)
        .where(Analysis.artefact_id.in_(all_artefact_ids))
    ).all()
    for output_path, details in rows:
        if output_path:
            # output_path may be absolute (/data/outputs/...) or relative;
            # normalise to a storage key prefix under 'outputs/'.
            if os.path.isabs(output_path):
                parts = output_path.rstrip('/').split('/')
                try:
                    idx = parts.index('outputs')
                    rel = '/'.join(parts[idx + 1:])
                except ValueError:
                    rel = '/'.join(parts[-3:]) if len(parts) >= 3 else parts[-1]
            else:
                rel = output_path
            output_dir_prefixes.append(f"outputs/{rel}")
        if details:
            try:
                parsed = json.loads(details)
                if 'outputs' in parsed and isinstance(parsed['outputs'], list):
                    for output in parsed['outputs']:
                        if 'filename' in output:
                            output_file_keys.append(f"outputs/{output['filename']}")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    return {
        'artefact_keys': artefact_keys,
        'output_file_keys': output_file_keys,
        'output_dir_prefixes': output_dir_prefixes,
        'cache_prefixes': cache_prefixes,
    }


def _background_cleanup_item_files(cleanup, storage, logger_name):
    """Delete item files in a background thread using the storage backend.

    Works with both LocalStorage and S3Storage since all paths are expressed
    as storage keys.  No ORM/app context needed.
    """
    import logging
    logger = logging.getLogger(logger_name)

    for key in cleanup['artefact_keys']:
        try:
            storage.delete(key)
        except Exception as e:
            logger.warning(f"Failed to delete artefact file {key}: {e}")

    for key in cleanup['output_file_keys']:
        try:
            storage.delete(key)
        except Exception as e:
            logger.warning(f"Failed to delete output file {key}: {e}")

    for prefix in cleanup['output_dir_prefixes']:
        try:
            storage.delete_prefix(prefix)
        except Exception as e:
            logger.warning(f"Failed to delete output directory {prefix}: {e}")

    for prefix in cleanup['cache_prefixes']:
        try:
            storage.delete_prefix(prefix)
        except Exception as e:
            logger.warning(f"Failed to delete cache {prefix}: {e}")


def _collect_all_item_ids(item):
    """Collect the item's ID and all descendant item IDs via recursive CTE."""
    from sqlalchemy import select as sa_select

    base = sa_select(Item.id).where(Item.parent_id == item.id)
    cte = base.cte(name='item_descendants', recursive=True)
    recursive = sa_select(Item.id).where(Item.parent_id == cte.c.id)
    cte = cte.union_all(recursive)
    descendant_ids = [r[0] for r in db.session.execute(sa_select(cte.c.id)).all()]
    return [item.id] + descendant_ids


def bulk_delete_item(item):
    """Delete an item, its descendants, and all related records using bulk SQL.

    Handles item hierarchy (parent/child items) by collecting all descendant
    items first, then bulk-deleting all artefacts across the entire tree.

    Replaces the previous approach of ORM cascade delete which loaded every
    related object into memory and emitted individual DELETEs — too slow for
    items with thousands of artefacts.

    File cleanup runs in a background daemon thread after the DB commit.
    """
    from ..database import ExternalReference, item_tags

    # Phase 1: Collect all item IDs (this item + all descendants)
    all_item_ids = _collect_all_item_ids(item)

    # Phase 2: Collect all artefact IDs via CTE
    all_ids = _collect_item_artefact_ids(all_item_ids)

    # Phase 3: Collect storage keys before deleting DB records
    cleanup = _collect_item_cleanup_keys(all_ids)
    storage = current_app.storage
    logger_name = current_app.logger.name

    # Phase 4: Bulk SQL deletes in FK-safe order
    if all_ids:
        # Null out derived_from_analysis_id before deleting analyses
        Artefact.query.filter(Artefact.id.in_(all_ids)).update(
            {Artefact.derived_from_analysis_id: None}, synchronize_session=False)

        _bulk_delete_artefact_dependents(all_ids)
        _bulk_delete_artefacts(all_ids)

    # Item-level children (external refs, tags for all items in hierarchy)
    ExternalReference.query.filter(
        ExternalReference.item_id.in_(all_item_ids)
    ).delete(synchronize_session=False)
    db.session.execute(
        item_tags.delete().where(item_tags.c.item_id.in_(all_item_ids)))

    # Break item self-referential FK, then delete all items
    Item.query.filter(Item.id.in_(all_item_ids)).update(
        {Item.parent_id: None}, synchronize_session=False)
    Item.query.filter(Item.id.in_(all_item_ids)).delete(
        synchronize_session=False)

    db.session.commit()

    # Phase 5: Background file cleanup via storage backend (no ORM needed)
    t = threading.Thread(
        target=_background_cleanup_item_files,
        args=(cleanup, storage, logger_name),
        daemon=True,
    )
    t.start()


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/delete', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/delete', methods=['POST'], endpoint='delete_nested')
@blueprint.route('/artefacts/<string:uuid>/delete', methods=['POST'], endpoint='delete_legacy')
@login_required
@require_permission('read_write')
def delete(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Delete an artefact and its file."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    item_url_id = artefact.item.url_id
    label = artefact.label

    # Delete files for this artefact and all derived artefacts.
    _delete_artefact_files(artefact)

    # Clean up analysis outputs (extraction trees, visualisations, cache).
    from shared.storage import S3Storage
    storage = current_app.storage
    if isinstance(storage, S3Storage):
        _cleanup_artefact_outputs_s3(artefact, storage)
    else:
        _cleanup_artefact_outputs(artefact, current_app.logger)

    db.session.delete(artefact)
    db.session.commit()

    flash(f'Artefact "{label}" deleted.', 'success')
    return redirect(url_for('myapp_blueprints_items.view', uuid=item_url_id))


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/download')
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/download', endpoint='download_nested')
@blueprint.route('/artefacts/<string:uuid>/download', endpoint='download_legacy')
@login_required
def download(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Download the artefact file.  Blocked when the artefact itself is
    restricted, or when any extracted file within it carries a restriction the
    user cannot bypass."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)

    restriction_redirect = _check_download_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    restriction_redirect = _check_artefact_file_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    storage = current_app.storage
    key = get_artefact_storage_key(artefact)

    # S3 mode: redirect to pre-signed URL
    url = storage.presigned_url(key, filename=artefact.original_filename)
    if url:
        return redirect(url)

    # Local mode: serve file directly
    full_path = get_artefact_path(artefact)
    if not os.path.exists(full_path):
        abort(404, description='File not found')

    return send_file(
        full_path,
        as_attachment=True,
        download_name=artefact.original_filename
    )


@blueprint.route('/files/<string:uuid>/download', endpoint='download_file')
@login_required
def download_file(uuid):
    """Download an individual extracted file from a partition.

    Honours artefact-level restrictions first, then file-level restrictions
    (including any restrictions on nested descendants of the file).
    """
    ef = ExtractedFile.query.filter_by(uuid=uuid).first_or_404()
    artefact = ef.partition.artefact

    restriction_redirect = _check_download_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    restriction_redirect = _check_file_download_restrictions(ef)
    if restriction_redirect:
        return restriction_redirect

    file_path = _resolve_extracted_file_path(ef)
    if not file_path:
        abort(404, description='Extracted file not found on disk')

    return send_file(file_path, as_attachment=True, download_name=ef.filename)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/restrictions', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/restrictions', methods=['POST'], endpoint='manage_restrictions_nested')
@login_required
@require_permission('read_write')
def manage_restrictions(item_id=None, artefact_id=None, root_id=None):
    """Add or remove a download restriction on an artefact."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id)

    action = request.form.get('action', '')
    category = request.form.get('category', '')
    reason = request.form.get('reason', '').strip() or None

    from ..database import RestrictionType, ArtefactRestriction
    try:
        rtype = RestrictionType(category)
    except (ValueError, KeyError):
        flash(f'Invalid restriction type: {category}', 'danger')
        return _redirect_to_artefact_view(artefact)

    if action == 'add':
        existing = ArtefactRestriction.query.filter_by(
            artefact_id=artefact.id, restriction_type=rtype
        ).first()
        if not existing:
            db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=rtype,
                reason=reason,
                added_by_id=current_user.id,
            ))
            db.session.commit()
            flash(f'Restriction added: {rtype.label}', 'success')
        else:
            flash(f'Restriction already exists: {rtype.label}', 'info')
    elif action == 'remove':
        existing = ArtefactRestriction.query.filter_by(
            artefact_id=artefact.id, restriction_type=rtype
        ).first()
        if existing:
            # Non-admins can only remove restrictions they added themselves
            if not current_user.is_admin and existing.added_by_id != current_user.id:
                flash('Only administrators can remove restrictions added by other users.', 'danger')
            else:
                db.session.delete(existing)
                db.session.commit()
                flash(f'Restriction removed: {rtype.label}', 'success')
        else:
            flash(f'Restriction not found: {rtype.label}', 'warning')
    elif action == 'update':
        new_category = request.form.get('new_category', '').strip()
        try:
            new_rtype = RestrictionType(new_category) if new_category else rtype
        except (ValueError, KeyError):
            flash(f'Invalid restriction type: {new_category}', 'danger')
            return _redirect_to_artefact_view(artefact)
        existing = ArtefactRestriction.query.filter_by(
            artefact_id=artefact.id, restriction_type=rtype
        ).first()
        if not existing:
            flash(f'Restriction not found: {rtype.label}', 'warning')
        elif not current_user.is_admin and existing.added_by_id != current_user.id:
            flash('Only administrators can edit restrictions added by other users.', 'danger')
        elif new_rtype != rtype and ArtefactRestriction.query.filter_by(
            artefact_id=artefact.id, restriction_type=new_rtype
        ).first():
            flash(f'A {new_rtype.label} restriction already exists.', 'danger')
        else:
            existing.restriction_type = new_rtype
            existing.reason = reason
            db.session.commit()
            flash('Restriction updated.', 'success')
    else:
        flash(f'Invalid action: {action}', 'danger')

    return _redirect_to_artefact_view(artefact)


@blueprint.route('/files/<string:uuid>/restrictions', methods=['POST'], endpoint='manage_file_restrictions')
@login_required
@require_permission('read_write')
def manage_file_restrictions(uuid):
    """Add or remove a restriction on an individual extracted file."""
    ef = ExtractedFile.query.filter_by(uuid=uuid).first_or_404()
    artefact = ef.partition.artefact
    # Always redirect to the root artefact so the user lands on the page that
    # shows the global restriction, even when the file belongs to a derived
    # artefact whose partition is displayed inline on the root's view.
    root_artefact = artefact.root_artefact

    action   = request.form.get('action', '')
    category = request.form.get('category', '')
    reason   = request.form.get('reason', '').strip() or None

    from ..database import RestrictionType
    try:
        rtype = RestrictionType(category)
    except (ValueError, KeyError):
        flash(f'Invalid restriction type: {category}', 'danger')
        return _redirect_to_artefact_view(root_artefact)

    if action == 'add':
        existing = ExtractedFileRestriction.query.filter_by(
            extracted_file_id=ef.id, restriction_type=rtype
        ).first()
        if not existing:
            db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=rtype,
                reason=reason,
                added_by_id=current_user.id,
            ))
            db.session.commit()
            flash(f'File restriction added: {rtype.label}', 'success')
        else:
            flash(f'File restriction already exists: {rtype.label}', 'info')
    elif action == 'remove':
        existing = ExtractedFileRestriction.query.filter_by(
            extracted_file_id=ef.id, restriction_type=rtype
        ).first()
        if existing:
            if not current_user.is_admin and existing.added_by_id != current_user.id:
                flash('Only administrators can remove restrictions added by other users.', 'danger')
            else:
                db.session.delete(existing)
                db.session.commit()
                flash(f'File restriction removed: {rtype.label}', 'success')
        else:
            flash(f'File restriction not found: {rtype.label}', 'warning')
    elif action == 'update':
        new_category = request.form.get('new_category', '').strip()
        try:
            new_rtype = RestrictionType(new_category) if new_category else rtype
        except (ValueError, KeyError):
            flash(f'Invalid restriction type: {new_category}', 'danger')
            return _redirect_to_artefact_view(artefact)
        existing = ExtractedFileRestriction.query.filter_by(
            extracted_file_id=ef.id, restriction_type=rtype
        ).first()
        if not existing:
            flash(f'File restriction not found: {rtype.label}', 'warning')
        elif not current_user.is_admin and existing.added_by_id != current_user.id:
            flash('Only administrators can edit restrictions added by other users.', 'danger')
        elif new_rtype != rtype and ExtractedFileRestriction.query.filter_by(
            extracted_file_id=ef.id, restriction_type=new_rtype
        ).first():
            flash(f'A {new_rtype.label} restriction already exists.', 'danger')
        else:
            existing.restriction_type = new_rtype
            existing.reason = reason
            db.session.commit()
            flash('File restriction updated.', 'success')
    else:
        flash(f'Invalid action: {action}', 'danger')

    return _redirect_to_artefact_view(root_artefact)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/analyse', methods=['GET', 'POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/analyse', methods=['GET', 'POST'], endpoint='analyse_nested')
@blueprint.route('/artefacts/<string:uuid>/analyse', methods=['GET', 'POST'], endpoint='analyse_legacy')
@login_required
@require_permission('read_write')
def analyse(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Re-run analysis on an artefact, clearing all previous results first."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    form = AnalyseForm()

    # Platform choices for hints
    platforms = Platform.query.order_by(Platform.name).all()
    form.platform_id.choices = [(0, '-- No hint --')] + [
        (p.id, p.name) for p in platforms
    ]

    if form.validate_on_submit():
        hints = {}
        if form.platform_id.data and form.platform_id.data != 0:
            platform = Platform.query.get(form.platform_id.data)
            if platform:
                hints['platform'] = platform.name
        if form.filesystem_hint.data:
            hints['filesystem'] = form.filesystem_hint.data
        if form.dfi_clock_mhz.data:
            hints['dfi_clock_mhz'] = form.dfi_clock_mhz.data
        if form.notes.data:
            hints['notes'] = form.notes.data

        cleanup = reset_artefact_for_reanalysis(artefact)
        queue_analyses_for_artefact(artefact, hints if hints else None)

        # Run filesystem cleanup in background so the redirect happens immediately.
        app = current_app._get_current_object()
        t = threading.Thread(
            target=_cleanup_analysis_outputs,
            args=(
                get_output_folder(),
                cleanup['output_files'],
                cleanup['output_dirs'],
                cleanup['cache_dir'],
                app.logger,
            ),
            daemon=True,
        )
        t.start()

        flash('Re-analysis queued. Previous results have been cleared.', 'success')
        return _redirect_to_artefact_view(artefact)

    # Pre-populate form with hints from the most recent analysis that had hints.
    if request.method == 'GET':
        last_with_hints = Analysis.query.filter(
            Analysis.artefact_id == artefact.id,
            Analysis.hints.isnot(None)
        ).order_by(Analysis.id.desc()).first()
        if last_with_hints:
            try:
                last_hints = json.loads(last_with_hints.hints)
                if 'platform' in last_hints:
                    platform = Platform.query.filter_by(name=last_hints['platform']).first()
                    if platform:
                        form.platform_id.data = platform.id
                if 'filesystem' in last_hints:
                    form.filesystem_hint.data = last_hints['filesystem']
                if 'dfi_clock_mhz' in last_hints:
                    form.dfi_clock_mhz.data = last_hints['dfi_clock_mhz']
                if 'notes' in last_hints:
                    form.notes.data = last_hints['notes']
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    # Show what analyses will be queued
    pending_types = ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])

    return render_template('artefacts/analyse.html',
                           form=form,
                           artefact=artefact,
                           pending_types=pending_types)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/compute-hashes', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/compute-hashes', methods=['POST'], endpoint='compute_hashes_route_nested')
@blueprint.route('/artefacts/<string:uuid>/compute-hashes', methods=['POST'], endpoint='compute_hashes_legacy')
@login_required
@require_permission('read_write')
def compute_hashes_route(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Compute file hashes for an artefact."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)

    if not artefact.storage_path:
        flash('File not found — artefact has no stored file.', 'error')
        return _redirect_to_artefact_view(artefact)

    key = get_artefact_storage_key(artefact)

    try:
        artefact.md5, artefact.sha256 = compute_file_hashes(key, use_storage=True)
        db.session.commit()
        flash('Hashes computed successfully.', 'success')
    except Exception as e:
        flash(f'Error computing hashes: {e}', 'error')

    return _redirect_to_artefact_view(artefact)

@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/rescan-hashes', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/rescan-hashes', methods=['POST'], endpoint='rescan_hashes_route_nested')
@blueprint.route('/artefacts/<string:uuid>/rescan-hashes', methods=['POST'], endpoint='rescan_hashes_legacy')
@login_required
@require_permission('read_write')
def rescan_hashes_route(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Re-link extracted files to active hash databases without re-analysing."""
    from ..utils.hash_rescan import rescan_hashes_for_artefact
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    updated, total = rescan_hashes_for_artefact(artefact)
    flash(f'Hash rescan complete: {updated} of {total} files updated.', 'success')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/<string:uuid>/rerun-product-recognition', methods=['POST'])
@login_required
@require_permission('read_write')
def rerun_product_recognition_route(uuid):
    """Queue PRODUCT_RECOGNITION for all partitions of an artefact without re-analysing."""
    from ..utils.hash_rescan import queue_product_recognition_for_partitions
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    partition_ids = [
        p.id for p in Partition.query.filter(
            Partition.artefact_id.in_(all_artefact_ids),
            Partition.total_files > 0,
        ).all()
    ]
    if not partition_ids:
        flash('No partitions with extracted files found.', 'warning')
        return _redirect_to_artefact_view(artefact)
    queued = queue_product_recognition_for_partitions(partition_ids)
    if queued:
        flash(f'Queued product recognition for {queued} partition(s).', 'success')
    else:
        flash('Product recognition already pending or running — nothing new queued.', 'info')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/outputs/<path:filename>')
@login_required
def get_output_file(filename):
    """Serve an analysis output file (visualisation, etc.) to logged-in users."""
    storage = current_app.storage
    key = storage.storage_key('outputs', filename)

    # S3 mode: redirect to pre-signed URL
    url = storage.presigned_url(key)
    if url:
        return redirect(url)

    # Local mode: serve directly with path traversal check
    folder = get_output_folder()
    file_path = os.path.realpath(os.path.join(folder, filename))
    if not file_path.startswith(os.path.realpath(folder) + os.sep):
        abort(404)
    if not os.path.exists(file_path):
        abort(404)
    mime, _ = mimetypes.guess_type(file_path)
    return send_file(file_path, mimetype=mime or 'application/octet-stream')


# vim: ts=4 sw=4 et
