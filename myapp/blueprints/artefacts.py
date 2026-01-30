"""
Arcology - Artefacts Blueprint

CRUD operations for digital artefacts.
"""

import os
import hashlib
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, send_file, abort
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional

from ..extensions import db
from ..database import (
    Item, Artefact, ArtefactType, Partition, ExtractedFile,
    Analysis, AnalysisType, AnalysisStatus
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/artefacts', template_folder='templates')


# =============================================================================
# Forms
# =============================================================================

class ArtefactForm(FlaskForm):
    label = StringField('Label', validators=[DataRequired()],
                        description='e.g., "Disc 1", "Program Disc", "Manual"')
    artefact_type = SelectField('Type',
                                 choices=[(t.value, t.name.replace('_', ' ').title())
                                          for t in ArtefactType],
                                 validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    file_path = StringField('File Path', validators=[DataRequired()],
                            description='Path to file on NAS')


class AnalysisRequestForm(FlaskForm):
    analysis_type = SelectField('Analysis Type',
                                 choices=[(t.value, t.name.replace('_', ' ').title())
                                          for t in AnalysisType],
                                 validators=[DataRequired()])
    tool_name = StringField('Tool', validators=[Optional()],
                            description='Specific tool to use (optional)')


class FileSearchForm(FlaskForm):
    filename = StringField('Filename', validators=[Optional()])
    extension = StringField('Extension', validators=[Optional()])
    md5 = StringField('MD5 Hash', validators=[Optional()])
    sha1 = StringField('SHA1 Hash', validators=[Optional()])
    show_known = BooleanField('Show known files', default=False)


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/<int:id>')
@login_required
def view(id):
    """View an artefact and its partitions/files."""
    artefact = Artefact.query.get_or_404(id)
    
    file_form = FileSearchForm(request.args)
    
    files_query = ExtractedFile.query.join(Partition).filter(
        Partition.artefact_id == artefact.id
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
                           files_pagination=files_pagination)


@blueprint.route('/item/<int:item_id>/new', methods=['GET', 'POST'])
@login_required
def new(item_id):
    """Add an artefact to an item."""
    item = Item.query.get_or_404(item_id)
    form = ArtefactForm()
    
    if form.validate_on_submit():
        file_path = form.file_path.data
        file_size = None
        
        full_path = os.path.join(
            current_app.config.get('NAS_BASE_PATH', ''),
            file_path.lstrip('/')
        )
        
        if os.path.exists(full_path):
            file_size = os.path.getsize(full_path)
        
        artefact = Artefact(
            item_id=item.id,
            label=form.label.data,
            artefact_type=ArtefactType(form.artefact_type.data),
            description=form.description.data,
            file_path=file_path,
            file_size=file_size
        )
        
        db.session.add(artefact)
        db.session.commit()
        
        flash(f'Artefact "{artefact.label}" added.', 'success')
        return redirect(url_for('myapp_blueprints_items.view', id=item.id))
    
    return render_template('artefacts/form.html', form=form, item=item, title='Add Artefact')


@blueprint.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    """Edit an artefact."""
    artefact = Artefact.query.get_or_404(id)
    form = ArtefactForm(obj=artefact)
    
    if request.method == 'GET':
        form.artefact_type.data = artefact.artefact_type.value
    
    if form.validate_on_submit():
        artefact.label = form.label.data
        artefact.artefact_type = ArtefactType(form.artefact_type.data)
        artefact.description = form.description.data
        artefact.file_path = form.file_path.data
        
        db.session.commit()
        
        flash(f'Artefact "{artefact.label}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    return render_template('artefacts/form.html',
                           form=form,
                           artefact=artefact,
                           item=artefact.item,
                           title='Edit Artefact')


@blueprint.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete(id):
    """Delete an artefact."""
    artefact = Artefact.query.get_or_404(id)
    item_id = artefact.item_id
    label = artefact.label
    
    db.session.delete(artefact)
    db.session.commit()
    
    flash(f'Artefact "{label}" deleted.', 'success')
    return redirect(url_for('myapp_blueprints_items.view', id=item_id))


@blueprint.route('/<int:id>/download')
@login_required
def download(id):
    """Download the artefact file."""
    artefact = Artefact.query.get_or_404(id)
    
    full_path = os.path.join(
        current_app.config.get('NAS_BASE_PATH', ''),
        artefact.file_path.lstrip('/')
    )
    
    if not os.path.exists(full_path):
        abort(404, description='File not found on storage')
    
    return send_file(
        full_path,
        as_attachment=True,
        download_name=os.path.basename(artefact.file_path)
    )


@blueprint.route('/<int:id>/analyse', methods=['GET', 'POST'])
@login_required
def request_analysis(id):
    """Request an analysis for an artefact."""
    artefact = Artefact.query.get_or_404(id)
    form = AnalysisRequestForm()
    
    if form.validate_on_submit():
        analysis = Analysis(
            artefact_id=artefact.id,
            analysis_type=AnalysisType(form.analysis_type.data),
            status=AnalysisStatus.PENDING,
            tool_name=form.tool_name.data or None
        )
        
        db.session.add(analysis)
        db.session.commit()
        
        flash(f'Analysis queued. Job ID: {analysis.id}', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    return render_template('artefacts/analyse.html', form=form, artefact=artefact)


@blueprint.route('/<int:id>/compute-hashes', methods=['POST'])
@login_required
def compute_hashes(id):
    """Compute file hashes for an artefact."""
    artefact = Artefact.query.get_or_404(id)
    
    full_path = os.path.join(
        current_app.config.get('NAS_BASE_PATH', ''),
        artefact.file_path.lstrip('/')
    )
    
    if not os.path.exists(full_path):
        flash('File not found on storage.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))
    
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()
    
    with open(full_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_hash.update(chunk)
            sha256_hash.update(chunk)
    
    artefact.md5 = md5_hash.hexdigest()
    artefact.sha256 = sha256_hash.hexdigest()
    artefact.file_size = os.path.getsize(full_path)
    
    db.session.commit()
    
    flash('Hashes computed successfully.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=artefact.id))


# vim: ts=4 sw=4 noet
