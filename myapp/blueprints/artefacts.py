"""
Arcology - Artefacts Blueprint

CRUD operations for digital artefacts with file upload and auto-analysis.
"""

import glob
import os
import hashlib
import shutil
import uuid
import json
import mimetypes
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, send_file, abort
from flask_login import login_required
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import StringField, TextAreaField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional
from werkzeug.utils import secure_filename

from ..extensions import db
from ..permissions import require_permission
from ..utils.slugs import generate_slug, ensure_unique_slug, lookup_by_identifier, lookup_artefact_by_id


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
    
    # Get extension
    _, ext = os.path.splitext(filename_lower)
    
    return EXTENSION_MAP.get(ext, ArtefactType.UNKNOWN)


# Analysis types appropriate for each artefact type
ANALYSIS_MAP = {
    # Flux images - visualisation and decode attempt
    ArtefactType.SCP: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    #ArtefactType.KF: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    #ArtefactType.FLUX_RAW: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE],
    
    # Sector-level floppy - file extraction only works on raw sector images
    # IMD is track-based format with metadata, HFE is an emulator container format
    # These need conversion to IMG (raw sectors) before file extraction can work
    ArtefactType.IMD: [AnalysisType.METADATA_EXTRACT, AnalysisType.FORMAT_IDENTIFY],
    ArtefactType.HFE: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.DISC_MASTERING_DETECT, AnalysisType.DISC_PROTECTION_DETECT],
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
    
    # Archives - extract contents
    ArtefactType.ZIP: [AnalysisType.FILE_EXTRACTION],
    ArtefactType.TARGZ: [AnalysisType.FILE_EXTRACTION],
    ArtefactType.RAR: [AnalysisType.FILE_EXTRACTION],
    
    # Unknown - try to identify
    ArtefactType.UNKNOWN: [AnalysisType.FORMAT_IDENTIFY],
}


def queue_analyses_for_artefact(artefact: Artefact, hints: dict = None, checksum_only: bool = False):
    """Queue appropriate analyses for an artefact based on its type.

    CHECKSUM_COMPUTE is always prepended as the first job regardless of artefact
    type; it does not need to appear in ANALYSIS_MAP.  Pass checksum_only=True
    to skip the type-specific analyses (used when auto-analyse is off on upload).
    """
    analysis_types = [AnalysisType.CHECKSUM_COMPUTE]
    if not checksum_only:
        analysis_types += ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])
    hints_json = json.dumps(hints) if hints else None
    
    for analysis_type in analysis_types:
        # Check if this analysis is already queued/running
        existing = Analysis.query.filter_by(
            artefact_id=artefact.id,
            analysis_type=analysis_type
        ).filter(Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING])).first()
        
        if not existing:
            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=analysis_type,
                status=AnalysisStatus.PENDING,
                hints=hints_json
            )
            db.session.add(analysis)
    
    db.session.commit()


# =============================================================================
# Forms
# =============================================================================

class ArtefactUploadForm(FlaskForm):
    """Form for uploading a new artefact."""
    file = FileField('File', validators=[FileRequired()])
    label = StringField('Label', validators=[DataRequired()],
                        description='e.g., "Disc 1", "Program Disc", "Manual"')
    artefact_type = SelectField('Type (auto-detected)', coerce=str, validators=[Optional()],
                                 description='Leave as "Auto-detect" unless incorrect')
    description = TextAreaField('Description', validators=[Optional()])
    auto_analyse = BooleanField('Run automatic analysis', default=True)


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
    notes = TextAreaField('Additional notes', validators=[Optional()])


class FileSearchForm(FlaskForm):
    partition_uuid = StringField('Partition UUID', validators=[Optional()])
    filename = StringField('Filename', validators=[Optional()])
    filetype = StringField('Filetype', validators=[Optional()])
    path = StringField('Path/Directory', validators=[Optional()])
    md5 = StringField('MD5 Hash', validators=[Optional()])
    sha1 = StringField('SHA1 Hash', validators=[Optional()])
    hide_known = BooleanField('Hide known files', default=False)
    show_directories = BooleanField('Show directories', default=False)
    recursive = BooleanField('Recursive (show all subdirs)', default=True)


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
    Files are stored in UPLOAD_FOLDER with a UUID-based name to avoid conflicts.
    """
    folder = get_upload_folder()

    # Generate unique storage name while preserving extension
    # Uses compound extension detection so drive.dd.zst -> <uuid>.dd.zst
    original_name = secure_filename(file.filename)
    ext = _get_storage_extension(original_name)
    storage_name = f"{uuid.uuid4().hex}{ext}"
    storage_path = os.path.join(folder, storage_name)
    
    # Save file
    file.save(storage_path)
    file_size = os.path.getsize(storage_path)
    
    # Return relative path for storage in DB
    return storage_name, file_size


def get_artefact_path(artefact: Artefact) -> str:
    """Get the full filesystem path for an artefact based on its storage directory."""
    if artefact.storage_directory == StorageDirectory.OUTPUTS:
        folder = get_output_folder()
    else:
        folder = get_upload_folder()
    return os.path.join(folder, artefact.storage_path)


def compute_file_hashes(filepath: str) -> tuple[str, str]:
    """Compute MD5 and SHA256 hashes for a file."""
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()
    
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_hash.update(chunk)
            sha256_hash.update(chunk)
    
    return md5_hash.hexdigest(), sha256_hash.hexdigest()


# =============================================================================
# Routes
# =============================================================================

def get_all_derived_artefact_ids(artefact: Artefact) -> list[int]:
    """Recursively collect all derived artefact IDs."""
    ids = []
    for derived in artefact.derived_artefacts:
        ids.append(derived.id)
        ids.extend(get_all_derived_artefact_ids(derived))
    return ids


def _collect_all_analyses(artefact: Artefact) -> list:
    """Recursively collect all analyses for an artefact and its derived artefacts."""
    analyses = list(artefact.analyses)
    for derived in artefact.derived_artefacts:
        analyses.extend(_collect_all_analyses(derived))
    return analyses


def reset_artefact_for_reanalysis(artefact: Artefact):
    """
    Reset an artefact to its just-uploaded state ready for re-analysis.

    Deletes all analyses, derived artefacts, and partitions (with their
    extracted file listings) for this artefact, then removes the associated
    files from disk.  The artefact's own uploaded file is preserved.

    This must be called before queueing new analyses when the user triggers
    a re-analyse, so that stale results from previous runs are fully cleared.
    """
    output_folder = get_output_folder()

    # Collect filesystem paths to clean up before deleting DB records.
    # analysis.output_path: extraction output directories (FILE_EXTRACTION etc.)
    # analysis.details outputs[]: named output files (flux visualisations etc.)
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
                current_app.logger.warning(f"Failed to parse analysis details during reset: {e}")

    # Delete storage files for all derived artefacts (recursively).
    # Must happen before the DB delete so we can still walk the ORM tree.
    for derived in artefact.derived_artefacts:
        _delete_artefact_files(derived)

    # Delete derived artefacts; cascades handle their analyses, partitions,
    # and extracted file records.
    for derived in list(artefact.derived_artefacts):
        db.session.delete(derived)

    # Delete analyses directly on this artefact.
    for analysis in list(artefact.analyses):
        db.session.delete(analysis)

    # Delete partitions directly on this artefact; cascade handles ExtractedFile.
    for partition in list(artefact.partitions):
        db.session.delete(partition)

    # Clear search index tables — protection/mastering rows are not cascade-deleted
    # with analyses, so must be explicitly removed so re-analysis starts fresh.
    ArtefactProtection.query.filter_by(artefact_id=artefact.id).delete()
    ArtefactMastering.query.filter_by(artefact_id=artefact.id).delete()

    db.session.commit()

    # Remove named output files (e.g., flux visualisation PNGs).
    for filename in output_files:
        path = os.path.join(output_folder, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
                current_app.logger.info(f"Deleted output file: {filename}")
            except Exception as e:
                current_app.logger.warning(f"Failed to delete output file {filename}: {e}")

    # Remove extraction output directories (e.g., extracted disc file trees).
    for path in output_dirs:
        if os.path.exists(path):
            try:
                shutil.rmtree(path)
                current_app.logger.info(f"Deleted output directory: {path}")
            except Exception as e:
                current_app.logger.warning(f"Failed to delete output directory {path}: {e}")

    # Remove cached decompressed partition images created by PARTITION_DETECT.
    cache_dir = os.path.join(output_folder, '.cache', artefact.uuid)
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            current_app.logger.info(f"Deleted partition cache: {cache_dir}")
        except Exception as e:
            current_app.logger.warning(f"Failed to delete partition cache {cache_dir}: {e}")


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>')
@login_required
def view(item_id, artefact_id):
    """View an artefact and its partitions/files."""
    item = lookup_by_identifier(Item, item_id)
    artefact = lookup_artefact_by_id(item, artefact_id)
    return _render_artefact_view(artefact)


@blueprint.route('/artefacts/<string:uuid>')
@login_required
def view_legacy(uuid):
    """Legacy flat-URL compat shim — resolves and renders without redirect."""
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    return _render_artefact_view(artefact)


def _render_artefact_view(artefact):

    # Only bind to request.args when the user has actively submitted a filter,
    # so that BooleanField defaults (e.g. recursive=True) apply on first load.
    # Without this, WTForms treats missing checkbox keys as False.
    _file_filter_keys = {'partition_uuid', 'filename', 'extension', 'path', 'md5', 'sha1',
                         'hide_known', 'show_directories', 'recursive'}
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

    # Fetch all related analyses for stats, newest first
    all_related_analyses = Analysis.query.filter(
        Analysis.artefact_id.in_(all_artefact_ids)
    ).order_by(Analysis.id.desc()).all()
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
                  if a.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)]
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

    # Hide directories by default (unless explicitly requested)
    if not file_form.show_directories.data:
        files_query = files_query.filter(ExtractedFile.is_directory == False)

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
        # Strip a leading '#' or '&' that users might include with the hex value
        ft = file_form.filetype.data.lower().lstrip('#&')
        files_query = files_query.filter(ExtractedFile.risc_os_filetype == ft)

    if file_form.path.data:
        path_filter = file_form.path.data.strip()
        if file_form.recursive.data:
            # Recursive: show all files under this path (starts with)
            files_query = files_query.filter(
                ExtractedFile.path.ilike(f'{path_filter}%')
            )
        else:
            # Non-recursive: only files directly in this directory (no additional slashes after path)
            # This shows files at the current level only
            from sqlalchemy import and_, not_, func
            files_query = files_query.filter(
                and_(
                    ExtractedFile.path.ilike(f'{path_filter}%'),
                    not_(func.substr(ExtractedFile.path, len(path_filter) + 1).contains('/'))
                )
            )

    if file_form.md5.data:
        files_query = files_query.filter(ExtractedFile.md5 == file_form.md5.data.lower())
    
    if file_form.sha1.data:
        files_query = files_query.filter(ExtractedFile.sha1 == file_form.sha1.data.lower())
    
    if file_form.hide_known.data:
        # Always show archive files even when hiding known files, because
        # archives serve as navigational pseudo-directories in the UI.
        from sqlalchemy import or_
        files_query = files_query.filter(
            or_(ExtractedFile.is_known == False, ExtractedFile.is_archive == True)
        )
    
    page = request.args.get('page', 1, type=int)
    valid_per_page = [25, 50, 100, 250]
    per_page_param = request.args.get('per_page', None, type=int)
    view_all = per_page_param == 0
    if per_page_param in valid_per_page:
        per_page = per_page_param
    elif view_all:
        per_page = 10000  # "view all" – large cap to avoid unbounded queries
        page = 1
    else:
        per_page = current_app.config.get('FILES_PER_PAGE', 100)

    # Column sorting: sort=<col> ascending, sort=-<col> descending
    sort_param = request.args.get('sort', 'path')
    sort_desc = sort_param.startswith('-')
    sort_col = sort_param.lstrip('-')
    _sort_columns = {
        'path': ExtractedFile.path,
        'size': ExtractedFile.file_size,
        'filetype': ExtractedFile.risc_os_filetype,
        'known': ExtractedFile.is_known,
    }
    sort_expr = _sort_columns.get(sort_col, ExtractedFile.path)
    if sort_desc:
        from sqlalchemy import desc
        sort_expr = desc(sort_expr)

    files_pagination = files_query.order_by(sort_expr).paginate(
        page=page, per_page=per_page, max_per_page=per_page
    )

    # Build query args for pagination links, preserving all active filters
    pagination_args = request.args.to_dict()
    pagination_args.pop('page', None)
    current_sort = sort_param

    # Extract subdirectories at the current path level for directory browsing
    current_path = file_form.path.data.strip() if file_form.path.data else ''
    subdirectories = set()

    if all_partitions:
        # Get all file paths matching current filter
        all_files = files_query.with_entities(ExtractedFile.path).all()

        for (file_path,) in all_files:
            # Remove the current path prefix
            if current_path:
                if not file_path.startswith(current_path):
                    continue
                relative_path = file_path[len(current_path):]
            else:
                relative_path = file_path

            # Extract the first directory component
            if '/' in relative_path:
                first_dir = relative_path.split('/')[0]
                if first_dir:  # Ignore empty strings
                    subdirectories.add(first_dir)

    subdirectories = sorted(subdirectories)

    # Build a set of archive file paths so the template can show archive
    # icons for "directories" that are actually archives.
    archive_paths = set()
    if all_partitions:
        archive_files = ExtractedFile.query.join(Partition).filter(
            Partition.artefact_id.in_(all_artefact_ids),
            ExtractedFile.is_archive == True
        ).with_entities(ExtractedFile.path).all()
        archive_paths = {af.path for af in archive_files}

    # Extract completed mastering and protection analysis results for display.
    # These are surfaced as badges + cards directly on the artefact view page.
    mastering_analysis = None
    protection_analysis = None
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
        if mastering_analysis is not None and protection_analysis is not None:
            break

    hashdb_mode = request.args.get('mode') == 'hashdb'

    # Recognised products for all partitions of this artefact tree
    recognised_products = []
    if all_partitions:
        partition_ids = [p.id for p in all_partitions]
        from sqlalchemy.orm import joinedload as _jl
        recognised_products = (
            RecognisedProduct.query
            .filter(RecognisedProduct.partition_id.in_(partition_ids))
            .options(_jl(RecognisedProduct.product).joinedload(KnownProduct.database))
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
                           valid_per_page=valid_per_page,
                           view_all=view_all,
                           all_partitions=all_partitions,
                           subdirectories=subdirectories,
                           current_path=current_path,
                           archive_paths=archive_paths,
                           current_sort=current_sort,
                           mastering_analysis=mastering_analysis,
                           protection_analysis=protection_analysis,
                           hashdb_mode=hashdb_mode,
                           recognised_products=recognised_products,
                           recognised_folder_paths=recognised_folder_paths,
                           hash_databases=hash_databases)


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

    if not file_ids:
        flash('No files selected.', 'warning')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid, mode='hashdb'))

    if raw_db_id == 'new':
        new_db_name = request.form.get('new_database_name', '').strip()
        if not new_db_name:
            flash('Provide a name for the new hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid, mode='hashdb'))
        database = HashDatabase(name=new_db_name)
        db.session.add(database)
        db.session.flush()
    else:
        try:
            database_id = int(raw_db_id)
        except (ValueError, TypeError):
            flash('Select a hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid, mode='hashdb'))
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
        return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid, mode='hashdb'))

    # Get OUTPUT_FOLDER for on-demand hash computation
    output_folder = current_app.config.get('OUTPUT_FOLDER', '')
    if not os.path.isabs(output_folder):
        output_folder = os.path.join(current_app.instance_path, output_folder)

    added = 0
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
            # Find the FILE_EXTRACTION analysis for this artefact with an output_path
            extraction = (
                Analysis.query
                .filter_by(artefact_id=ef.partition.artefact_id, analysis_type=AnalysisType.FILE_EXTRACTION)
                .filter(Analysis.output_path.isnot(None), Analysis.status == AnalysisStatus.COMPLETED)
                .first()
            )
            if extraction and extraction.output_path:
                # output_path may be absolute (from worker) or relative to OUTPUT_FOLDER
                base = extraction.output_path
                if not os.path.isabs(base):
                    base = os.path.join(output_folder, base)
                # File path within the extraction directory
                file_path_on_disk = os.path.join(base, ef.path.lstrip('/'))
                # For Acorn archives the worker stores the RISC OS display
                # name in ef.path (e.g. "!Boot,ffe/!Run") but the actual
                # file on disk has a filetype suffix ("!Boot,ffe/!Run,feb8").
                # Fall back to a glob if the exact path isn't found.
                if not os.path.isfile(file_path_on_disk):
                    candidates = [
                        f for f in glob.glob(file_path_on_disk + ',*')
                        if os.path.isfile(f)
                    ]
                    if candidates:
                        file_path_on_disk = candidates[0]
                    else:
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
            else:
                skipped_no_hash.append(ef.path)
                continue

        # Deduplicate: skip if this md5 already exists in the database
        if KnownFile.query.filter_by(database_id=database.id, md5=md5).first():
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
            relative_path=ef.path or None,
        )
        db.session.add(kf)
        added += 1

    database.file_count = (database.file_count or 0) + added
    db.session.commit()

    if added:
        flash(f'Added {added} file(s) to "{product.title}" in "{database.name}".', 'success')
    if skipped_no_hash:
        flash(f'{len(skipped_no_hash)} file(s) skipped — no hash available and extraction analysis not found. Re-run FILE_EXTRACTION first.', 'warning')
    if skipped_no_file:
        flash(f'{len(skipped_no_file)} file(s) skipped — extracted files no longer on disk.', 'warning')
    if not added and not skipped_no_hash and not skipped_no_file:
        flash('All selected files already exist in this hash database.', 'info')

    return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid, mode='hashdb'))


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
            mime_type=mime_type
        )
        
        db.session.add(artefact)
        db.session.commit()

        # Generate slug (unique within this item)
        base_slug = generate_slug(artefact.label)
        artefact.slug = ensure_unique_slug(base_slug, Artefact, scope_filter={'item_id': item.id})
        db.session.commit()

        # Always queue checksum computation via the worker; also queue type-specific
        # analyses if the user requested auto-analyse.
        queue_analyses_for_artefact(artefact, checksum_only=not form.auto_analyse.data)
        if form.auto_analyse.data:
            flash(f'Artefact "{artefact.label}" uploaded. Analysis queued.', 'success')
        else:
            flash(f'Artefact "{artefact.label}" uploaded.', 'success')

        return redirect(url_for(f'{ROUTENAME}.view', item_id=item.url_id, artefact_id=artefact.url_slug))

    return render_template('artefacts/upload.html', form=form, item=item)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/edit', methods=['GET', 'POST'])
@blueprint.route('/artefacts/<string:uuid>/edit', methods=['GET', 'POST'], endpoint='edit_legacy')
@login_required
@require_permission('read_write')
def edit(item_id=None, artefact_id=None, uuid=None):
    """Edit artefact metadata."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)
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
        return redirect(url_for(f'{ROUTENAME}.view', item_id=artefact.item.url_id, artefact_id=artefact.url_slug))

    return render_template('artefacts/edit.html',
                           form=form,
                           artefact=artefact,
                           item=artefact.item)


def _delete_artefact_files(artefact):
    """Recursively delete files for an artefact and all its derived artefacts."""
    for derived in artefact.derived_artefacts:
        _delete_artefact_files(derived)
    try:
        full_path = get_artefact_path(artefact)
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete file for artefact {artefact.uuid}: {e}")


def _delete_item_files(item):
    """Delete all files associated with an item's artefacts before DB cascade delete.

    For each artefact, removes the stored file (recursing into derived artefacts),
    analysis output directories, named output files, and cached partition images.
    Must be called while the ORM relationships are still intact (before db.session.delete).
    """
    output_folder = get_output_folder()

    for artefact in item.artefacts:
        # Collect analysis outputs before we lose the ORM tree.
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
                    current_app.logger.warning(f"Failed to parse analysis details during item delete: {e}")

        # Delete stored files for this artefact and all its derived artefacts.
        _delete_artefact_files(artefact)

        # Remove named output files (e.g., flux visualisation PNGs).
        for filename in output_files:
            path = os.path.join(output_folder, filename)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    current_app.logger.info(f"Deleted output file: {filename}")
                except Exception as e:
                    current_app.logger.warning(f"Failed to delete output file {filename}: {e}")

        # Remove extraction output directories (e.g., extracted disc file trees).
        for path in output_dirs:
            if os.path.exists(path):
                try:
                    shutil.rmtree(path)
                    current_app.logger.info(f"Deleted output directory: {path}")
                except Exception as e:
                    current_app.logger.warning(f"Failed to delete output directory {path}: {e}")

        # Remove cached decompressed partition images.
        cache_dir = os.path.join(output_folder, '.cache', artefact.uuid)
        if os.path.exists(cache_dir):
            try:
                shutil.rmtree(cache_dir)
                current_app.logger.info(f"Deleted partition cache: {cache_dir}")
            except Exception as e:
                current_app.logger.warning(f"Failed to delete partition cache {cache_dir}: {e}")


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/delete', methods=['POST'])
@blueprint.route('/artefacts/<string:uuid>/delete', methods=['POST'], endpoint='delete_legacy')
@login_required
@require_permission('read_write')
def delete(item_id=None, artefact_id=None, uuid=None):
    """Delete an artefact and its file."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)
    item_url_id = artefact.item.url_id
    label = artefact.label

    # Delete files for this artefact and all derived artefacts
    _delete_artefact_files(artefact)

    db.session.delete(artefact)
    db.session.commit()

    flash(f'Artefact "{label}" deleted.', 'success')
    return redirect(url_for('myapp_blueprints_items.view', uuid=item_url_id))


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/download')
@blueprint.route('/artefacts/<string:uuid>/download', endpoint='download_legacy')
@login_required
def download(item_id=None, artefact_id=None, uuid=None):
    """Download the artefact file."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)

    full_path = get_artefact_path(artefact)

    if not os.path.exists(full_path):
        abort(404, description='File not found')

    return send_file(
        full_path,
        as_attachment=True,
        download_name=artefact.original_filename
    )


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/analyse', methods=['GET', 'POST'])
@blueprint.route('/artefacts/<string:uuid>/analyse', methods=['GET', 'POST'], endpoint='analyse_legacy')
@login_required
@require_permission('read_write')
def analyse(item_id=None, artefact_id=None, uuid=None):
    """Re-run analysis on an artefact, clearing all previous results first."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)
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
        if form.notes.data:
            hints['notes'] = form.notes.data

        reset_artefact_for_reanalysis(artefact)
        queue_analyses_for_artefact(artefact, hints if hints else None)

        flash('Re-analysis queued. Previous results have been cleared.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', item_id=artefact.item.url_id, artefact_id=artefact.url_slug))

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
@blueprint.route('/artefacts/<string:uuid>/compute-hashes', methods=['POST'], endpoint='compute_hashes_legacy')
@login_required
@require_permission('read_write')
def compute_hashes_route(item_id=None, artefact_id=None, uuid=None):
    """Compute file hashes for an artefact."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)

    full_path = get_artefact_path(artefact)

    if not os.path.exists(full_path):
        flash('File not found.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', item_id=artefact.item.url_id, artefact_id=artefact.url_slug))

    try:
        artefact.md5, artefact.sha256 = compute_file_hashes(full_path)
        db.session.commit()
        flash('Hashes computed successfully.', 'success')
    except Exception as e:
        flash(f'Error computing hashes: {e}', 'error')

    return redirect(url_for(f'{ROUTENAME}.view', item_id=artefact.item.url_id, artefact_id=artefact.url_slug))

@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/rescan-hashes', methods=['POST'])
@blueprint.route('/artefacts/<string:uuid>/rescan-hashes', methods=['POST'], endpoint='rescan_hashes_legacy')
@login_required
@require_permission('read_write')
def rescan_hashes_route(item_id=None, artefact_id=None, uuid=None):
    """Re-link extracted files to active hash databases without re-analysing."""
    from ..utils.hash_rescan import rescan_hashes_for_artefact
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    else:
        item = lookup_by_identifier(Item, item_id)
        artefact = lookup_artefact_by_id(item, artefact_id)
    updated, total = rescan_hashes_for_artefact(artefact)
    flash(f'Hash rescan complete: {updated} of {total} files updated.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', item_id=artefact.item.url_id, artefact_id=artefact.url_slug))



# vim: ts=4 sw=4 et
