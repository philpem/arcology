"""
Arcology - Artefacts Blueprint

CRUD operations for digital artefacts with file upload and auto-analysis.
"""

import os
import hashlib
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
from ..database import (
    Item, Artefact, ArtefactType, Partition, ExtractedFile,
    Analysis, AnalysisType, AnalysisStatus, Platform, StorageDirectory
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/artefacts', template_folder='templates')


# =============================================================================
# Type Detection
# =============================================================================

# Extension to ArtefactType mapping
EXTENSION_MAP = {
    # Flux-level
    '.scp': ArtefactType.SCP,
    '.kf': ArtefactType.KF,
    '.kfraw': ArtefactType.KF,
    '.ipf': ArtefactType.IPF,
    '.raw': ArtefactType.FLUX_RAW,
    
    # Sector-level floppy
    '.imd': ArtefactType.IMD,
    '.td0': ArtefactType.TD0,
    '.hfe': ArtefactType.HFE,
    '.d64': ArtefactType.D64,
    '.adf': ArtefactType.ADF,
    '.dsk': ArtefactType.DSK,
    '.img': ArtefactType.IMG,
    '.ima': ArtefactType.IMG,
    
    # CD/DVD
    '.iso': ArtefactType.ISO,
    '.bin': ArtefactType.BIN_CUE,
    '.cue': ArtefactType.BIN_CUE,
    '.mdf': ArtefactType.MDF_MDS,
    '.mds': ArtefactType.MDF_MDS,
    '.nrg': ArtefactType.NRG,
    
    # Hard drive raw images
    '.dd': ArtefactType.DD,
    
    # Documents
    '.pdf': ArtefactType.PDF,
    '.djvu': ArtefactType.DJVU,
    
    # Images
    '.jpg': ArtefactType.JPEG,
    '.jpeg': ArtefactType.JPEG,
    '.png': ArtefactType.PNG,
    '.tif': ArtefactType.TIFF,
    '.tiff': ArtefactType.TIFF,
    
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
    ArtefactType.KF: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    ArtefactType.IPF: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_LISTING],
    ArtefactType.FLUX_RAW: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE],
    
    # Sector-level floppy - file listing only works on raw sector images
    # IMD is track-based format with metadata, HFE is an emulator container format
    # These need conversion to IMG (raw sectors) before file listing/extraction can work
    ArtefactType.IMD: [AnalysisType.METADATA_EXTRACT, AnalysisType.FORMAT_IDENTIFY],
    ArtefactType.TD0: [AnalysisType.METADATA_EXTRACT, AnalysisType.FORMAT_IDENTIFY],
    ArtefactType.HFE: [AnalysisType.FORMAT_IDENTIFY],
    ArtefactType.D64: [AnalysisType.FILE_LISTING],
    ArtefactType.ADF: [AnalysisType.FILE_LISTING],
    ArtefactType.DSK: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.FILE_LISTING],
    ArtefactType.IMG: [AnalysisType.FORMAT_IDENTIFY, AnalysisType.FILE_LISTING],
    
    # CD/DVD - file listing
    ArtefactType.ISO: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_LISTING],
    ArtefactType.BIN_CUE: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_LISTING],
    ArtefactType.MDF_MDS: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_LISTING],
    ArtefactType.NRG: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_LISTING],
    
    # Hard drive raw images - partition detection then file listing
    ArtefactType.DD: [AnalysisType.PARTITION_DETECT, AnalysisType.FILE_LISTING],
    ArtefactType.DD_ZST: [AnalysisType.PARTITION_DETECT, AnalysisType.FILE_LISTING],
    ArtefactType.DD_GZ: [AnalysisType.PARTITION_DETECT, AnalysisType.FILE_LISTING],
    ArtefactType.DD_BZ2: [AnalysisType.PARTITION_DETECT, AnalysisType.FILE_LISTING],
    
    # Documents/images - just metadata/checksums
    ArtefactType.PDF: [AnalysisType.METADATA_EXTRACT],
    ArtefactType.DJVU: [AnalysisType.METADATA_EXTRACT],
    ArtefactType.JPEG: [AnalysisType.METADATA_EXTRACT],
    ArtefactType.PNG: [AnalysisType.METADATA_EXTRACT],
    ArtefactType.TIFF: [AnalysisType.METADATA_EXTRACT],
    
    # Archives - list contents
    ArtefactType.ZIP: [AnalysisType.FILE_LISTING],
    ArtefactType.TARGZ: [AnalysisType.FILE_LISTING],
    ArtefactType.RAR: [AnalysisType.FILE_LISTING],
    
    # Unknown - try to identify
    ArtefactType.UNKNOWN: [AnalysisType.FORMAT_IDENTIFY],
}


def queue_analyses_for_artefact(artefact: Artefact, hints: dict = None):
    """Queue appropriate analyses for an artefact based on its type."""
    analysis_types = ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])
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


class AnalyseForm(FlaskForm):
    """Form for running analysis with optional hints."""
    platform_id = SelectField('Platform hint', coerce=int, validators=[Optional()],
                               description='Helps analysis tools identify format')
    filesystem_hint = StringField('Filesystem hint', validators=[Optional()],
                                   description='e.g., adfs, fat12, hfs')
    notes = TextAreaField('Additional notes', validators=[Optional()])


class FileSearchForm(FlaskForm):
    filename = StringField('Filename', validators=[Optional()])
    extension = StringField('Extension', validators=[Optional()])
    md5 = StringField('MD5 Hash', validators=[Optional()])
    sha1 = StringField('SHA1 Hash', validators=[Optional()])
    show_known = BooleanField('Show known files', default=False)


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


def save_uploaded_file(file) -> tuple[str, int]:
    """
    Save an uploaded file and return (storage_path, file_size).
    Files are stored in UPLOAD_FOLDER with a UUID-based name to avoid conflicts.
    """
    folder = get_upload_folder()
    
    # Generate unique storage name while preserving extension
    original_name = secure_filename(file.filename)
    _, ext = os.path.splitext(original_name)
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


@blueprint.route('/<int:id>')
@login_required
def view(id):
    """View an artefact and its partitions/files."""
    artefact = Artefact.query.get_or_404(id)

    file_form = FileSearchForm(request.args)

    # Collect all artefact IDs: current + all derived (recursively)
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)

    # Query partitions from all artefacts (for display)
    all_partitions = Partition.query.filter(
        Partition.artefact_id.in_(all_artefact_ids)
    ).order_by(Partition.artefact_id, Partition.partition_index).all()

    files_query = ExtractedFile.query.join(Partition).filter(
        Partition.artefact_id.in_(all_artefact_ids)
    )
    
    if file_form.filename.data:
        files_query = files_query.filter(
            ExtractedFile.filename.ilike(f'%{file_form.filename.data}%')
        )
    
    if file_form.extension.data:
        files_query = files_query.filter(
            ExtractedFile.extension == file_form.extension.data.lower().lstrip('.')
        )
    
    if file_form.md5.data:
        files_query = files_query.filter(ExtractedFile.md5 == file_form.md5.data.lower())
    
    if file_form.sha1.data:
        files_query = files_query.filter(ExtractedFile.sha1 == file_form.sha1.data.lower())
    
    if not file_form.show_known.data:
        files_query = files_query.filter(ExtractedFile.is_known == False)
    
    page = request.args.get('page', 1, type=int)
    per_page = current_app.config.get('FILES_PER_PAGE', 100)
    files_pagination = files_query.order_by(ExtractedFile.path).paginate(
        page=page, per_page=per_page
    )
    
    return render_template('artefacts/view.html',
                           artefact=artefact,
                           file_form=file_form,
                           files=files_pagination.items,
                           files_pagination=files_pagination,
                           all_partitions=all_partitions)


@blueprint.route('/item/<int:item_id>/upload', methods=['GET', 'POST'])
@login_required
def upload(item_id):
    """Upload a new artefact."""
    item = Item.query.get_or_404(item_id)
    form = ArtefactUploadForm()
    
    # Build type choices with auto-detect as default
    type_choices = [('auto', '-- Auto-detect --')]
    type_choices.extend([(t.value, t.value.upper().replace('_', ' ')) for t in ArtefactType if t != ArtefactType.UNKNOWN])
    form.artefact_type.choices = type_choices
    
    if form.validate_on_submit():
        file = form.file.data
        original_filename = secure_filename(file.filename)
        
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
        
        # Compute hashes immediately for small files
        if file_size < 100 * 1024 * 1024:  # < 100MB
            try:
                full_path = get_artefact_path(artefact)
                artefact.md5, artefact.sha256 = compute_file_hashes(full_path)
                db.session.commit()
            except Exception as e:
                current_app.logger.warning(f"Failed to compute hashes: {e}")
        
        # Queue automatic analysis
        if form.auto_analyse.data:
            queue_analyses_for_artefact(artefact)
            flash(f'Artefact "{artefact.label}" uploaded. Analysis queued.', 'success')
        else:
            flash(f'Artefact "{artefact.label}" uploaded.', 'success')
        
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    return render_template('artefacts/upload.html', form=form, item=item)


@blueprint.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    """Edit artefact metadata."""
    artefact = Artefact.query.get_or_404(id)
    form = ArtefactEditForm(obj=artefact)
    
    # Build type choices
    type_choices = [(t.value, t.value.upper().replace('_', ' ')) for t in ArtefactType]
    form.artefact_type.choices = type_choices
    
    if request.method == 'GET':
        form.artefact_type.data = artefact.artefact_type.value
    
    if form.validate_on_submit():
        artefact.label = form.label.data
        new_type = ArtefactType(form.artefact_type.data)
        if new_type != artefact.artefact_type:
            artefact.artefact_type = new_type
            artefact.type_overridden = True
        artefact.description = form.description.data
        
        db.session.commit()
        
        flash(f'Artefact "{artefact.label}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    return render_template('artefacts/edit.html',
                           form=form,
                           artefact=artefact,
                           item=artefact.item)


@blueprint.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete(id):
    """Delete an artefact and its file."""
    artefact = Artefact.query.get_or_404(id)
    item_id = artefact.item_id
    label = artefact.label
    
    # Delete the actual file
    try:
        full_path = get_artefact_path(artefact)
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception as e:
        current_app.logger.warning(f"Failed to delete file: {e}")
    
    db.session.delete(artefact)
    db.session.commit()
    
    flash(f'Artefact "{label}" deleted.', 'success')
    return redirect(url_for('myapp_blueprints_items.view', id=item_id))


@blueprint.route('/<int:id>/download')
@login_required
def download(id):
    """Download the artefact file."""
    artefact = Artefact.query.get_or_404(id)
    
    full_path = get_artefact_path(artefact)
    
    if not os.path.exists(full_path):
        abort(404, description='File not found')
    
    return send_file(
        full_path,
        as_attachment=True,
        download_name=artefact.original_filename
    )


@blueprint.route('/<int:id>/analyse', methods=['GET', 'POST'])
@login_required
def analyse(id):
    """Run analysis on an artefact with optional hints."""
    artefact = Artefact.query.get_or_404(id)
    form = AnalyseForm()
    
    # Platform choices for hints
    form.platform_id.choices = [(0, '-- No hint --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
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
        
        queue_analyses_for_artefact(artefact, hints if hints else None)
        
        flash('Analysis queued.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    # Show what analyses will be queued
    pending_types = ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])
    
    return render_template('artefacts/analyse.html',
                           form=form,
                           artefact=artefact,
                           pending_types=pending_types)


@blueprint.route('/<int:id>/compute-hashes', methods=['POST'])
@login_required
def compute_hashes_route(id):
    """Compute file hashes for an artefact."""
    artefact = Artefact.query.get_or_404(id)
    
    full_path = get_artefact_path(artefact)
    
    if not os.path.exists(full_path):
        flash('File not found.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    try:
        artefact.md5, artefact.sha256 = compute_file_hashes(full_path)
        db.session.commit()
        flash('Hashes computed successfully.', 'success')
    except Exception as e:
        flash(f'Error computing hashes: {e}', 'error')
    
    return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))


# vim: ts=4 sw=4 noet
