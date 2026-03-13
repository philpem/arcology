"""
Arcology - Hash Database Blueprint

Hash databases, known products, and file recognition.
"""

import csv
import io
import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, Response
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional, Length

from ..extensions import db
from ..database import Platform, HashDatabase, KnownProduct, KnownFile
from ..permissions import require_permission

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/hashdb', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("HashDB", f"{ROUTENAME}.index", 250)


# =============================================================================
# Forms
# =============================================================================

class HashDatabaseForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    source_url = StringField('Source URL', validators=[Optional()])
    version = StringField('Version', validators=[Optional(), Length(max=50)])
    platform_id = SelectField('Platform', coerce=int, validators=[Optional()])
    enable_product_recognition = BooleanField('Folder recognition')


def _platform_choices():
    return [(0, '-- All Platforms --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]


# =============================================================================
# Index / List
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    databases = HashDatabase.query.order_by(HashDatabase.name).all()
    return render_template('hashdb/index.html', databases=databases)


# =============================================================================
# Create
# =============================================================================

@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new():
    form = HashDatabaseForm()
    form.platform_id.choices = _platform_choices()

    if form.validate_on_submit():
        database = HashDatabase(
            name=form.name.data,
            description=form.description.data,
            source_url=form.source_url.data,
            version=form.version.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            enable_product_recognition=form.enable_product_recognition.data,
        )
        db.session.add(database)
        db.session.commit()
        flash(f'Hash database "{database.name}" created.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=database.id))

    return render_template('hashdb/new.html', form=form)


# =============================================================================
# View / Edit / Delete
# =============================================================================

@blueprint.route('/<int:id>')
@login_required
def view(id):
    database = HashDatabase.query.get_or_404(id)
    products = KnownProduct.query.filter_by(database_id=id).order_by(KnownProduct.title).all()
    platforms = Platform.query.order_by(Platform.name).all()
    return render_template('hashdb/view.html',
                           database=database,
                           products=products,
                           platforms=platforms)


@blueprint.route('/<int:id>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit(id):
    database = HashDatabase.query.get_or_404(id)
    form = HashDatabaseForm(obj=database)
    form.platform_id.choices = _platform_choices()
    if form.validate_on_submit():
        database.name = form.name.data
        database.description = form.description.data
        database.source_url = form.source_url.data
        database.version = form.version.data
        database.platform_id = form.platform_id.data if form.platform_id.data != 0 else None
        database.enable_product_recognition = form.enable_product_recognition.data
        db.session.commit()
        flash('Hash database updated.', 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    return redirect(url_for(f'{ROUTENAME}.view', id=id))


@blueprint.route('/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete(id):
    database = HashDatabase.query.get_or_404(id)
    name = database.name
    db.session.delete(database)
    db.session.commit()
    flash(f'Hash database "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


# =============================================================================
# Toggle folder recognition (one-click)
# =============================================================================

@blueprint.route('/<int:id>/toggle-recognition', methods=['POST'])
@login_required
@require_permission('read_write')
def toggle_recognition(id):
    database = HashDatabase.query.get_or_404(id)
    database.enable_product_recognition = not database.enable_product_recognition
    db.session.commit()
    state = 'enabled' if database.enable_product_recognition else 'disabled'
    flash(f'Folder recognition {state} for "{database.name}".', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=id))


# =============================================================================
# Export
# =============================================================================

@blueprint.route('/<int:id>/export')
@login_required
def export(id):
    database = HashDatabase.query.get_or_404(id)
    fmt = request.args.get('format', 'json').lower()
    products = KnownProduct.query.filter_by(database_id=id).order_by(KnownProduct.title).all()

    if fmt == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['product_title', 'filename', 'file_size', 'md5', 'sha1', 'sha256',
                         'crc32', 'is_required', 'relative_path', 'description'])
        for product in products:
            for kf in product.known_files:
                writer.writerow([
                    product.title, kf.filename, kf.file_size or '',
                    kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                    '1' if kf.is_required else '0',
                    kf.relative_path or '', kf.description or '',
                ])
        for kf in KnownFile.query.filter_by(database_id=id, product_id=None).all():
            writer.writerow([
                '', kf.filename, kf.file_size or '',
                kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                '1' if kf.is_required else '0',
                kf.relative_path or '', kf.description or '',
            ])
        filename = f"{database.name.replace(' ', '_')}.csv"
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    # JSON
    data = {
        'schema_version': 1,
        'database': {
            'name': database.name,
            'description': database.description,
            'version': database.version,
            'source_url': database.source_url,
        },
        'products': [
            {
                'title': p.title,
                'description': p.description,
                'path_match_enabled': p.path_match_enabled,
                'files': [
                    {
                        'filename': kf.filename,
                        'file_size': kf.file_size,
                        'md5': kf.md5,
                        'sha1': kf.sha1,
                        'sha256': kf.sha256,
                        'crc32': kf.crc32,
                        'is_required': kf.is_required,
                        'relative_path': kf.relative_path,
                        'description': kf.description,
                    }
                    for kf in p.known_files
                ],
            }
            for p in products
        ],
    }
    filename = f"{database.name.replace(' ', '_')}.json"
    return Response(json.dumps(data, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# =============================================================================
# Import
# =============================================================================

@blueprint.route('/import', methods=['POST'])
@login_required
@require_permission('read_write')
def import_database():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file uploaded.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.index'))

    name_override = request.form.get('name', '').strip()
    merge = 'merge' in request.form

    filename_lower = f.filename.lower()
    if filename_lower.endswith('.json'):
        fmt = 'json'
    elif filename_lower.endswith('.csv'):
        fmt = 'csv'
    else:
        flash('Unknown format — use a .json or .csv file.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.index'))

    try:
        content = f.read().decode('utf-8')
    except Exception as e:
        flash(f'Could not read file: {e}', 'danger')
        return redirect(url_for(f'{ROUTENAME}.index'))

    if fmt == 'json':
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            flash(f'Invalid JSON: {e}', 'danger')
            return redirect(url_for(f'{ROUTENAME}.index'))

        db_info = data.get('database', {})
        db_name = name_override or db_info.get('name', '').strip()
        if not db_name:
            flash('The JSON file has no database name; provide one in the Name field.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.index'))

        database = HashDatabase.query.filter_by(name=db_name).first()
        if database and not merge:
            flash(f'"{db_name}" already exists. Tick "Merge into existing" to add to it.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.index'))
        if not database:
            database = HashDatabase(
                name=db_name,
                description=db_info.get('description'),
                version=db_info.get('version'),
                source_url=db_info.get('source_url'),
            )
            db.session.add(database)
            db.session.flush()

        products_added = files_added = 0
        for p_data in data.get('products', []):
            p_title = (p_data.get('title') or '').strip()
            if not p_title:
                continue
            product = KnownProduct.query.filter_by(database_id=database.id, title=p_title).first()
            if not product:
                product = KnownProduct(
                    database_id=database.id,
                    title=p_title,
                    description=p_data.get('description'),
                    path_match_enabled=p_data.get('path_match_enabled', False),
                )
                db.session.add(product)
                db.session.flush()
                products_added += 1
            for f_data in p_data.get('files', []):
                md5 = (f_data.get('md5') or '').strip().lower() or None
                if md5 and KnownFile.query.filter_by(database_id=database.id, md5=md5).first():
                    continue
                kf = KnownFile(
                    database_id=database.id,
                    product_id=product.id,
                    filename=f_data.get('filename', ''),
                    file_size=f_data.get('file_size'),
                    md5=md5,
                    sha1=(f_data.get('sha1') or '').strip().lower() or None,
                    sha256=(f_data.get('sha256') or '').strip().lower() or None,
                    crc32=(f_data.get('crc32') or '').strip().lower() or None,
                    is_required=f_data.get('is_required', True),
                    relative_path=f_data.get('relative_path') or None,
                    description=f_data.get('description') or None,
                )
                db.session.add(kf)
                files_added += 1
        database.file_count = (database.file_count or 0) + files_added
        db.session.commit()
        flash(f'Imported {products_added} product(s) and {files_added} file(s) into "{database.name}".', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=database.id))

    else:  # CSV
        db_name = name_override
        if not db_name:
            flash('A database name is required for CSV import.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.index'))

        database = HashDatabase.query.filter_by(name=db_name).first()
        if database and not merge:
            flash(f'"{db_name}" already exists. Tick "Merge into existing" to add to it.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.index'))
        if not database:
            database = HashDatabase(name=db_name)
            db.session.add(database)
            db.session.flush()

        reader = csv.DictReader(io.StringIO(content))
        product_cache: dict[str, KnownProduct] = {}
        files_added = 0
        for row in reader:
            p_title = (row.get('product_title') or '').strip()
            if not p_title:
                continue
            if p_title not in product_cache:
                product = KnownProduct.query.filter_by(database_id=database.id, title=p_title).first()
                if not product:
                    product = KnownProduct(database_id=database.id, title=p_title)
                    db.session.add(product)
                    db.session.flush()
                product_cache[p_title] = product
            product = product_cache[p_title]

            md5 = (row.get('md5') or '').strip().lower() or None
            if md5 and KnownFile.query.filter_by(database_id=database.id, md5=md5).first():
                continue

            file_size_str = (row.get('file_size') or '').strip()
            try:
                file_size = int(file_size_str) if file_size_str else None
            except ValueError:
                file_size = None

            kf = KnownFile(
                database_id=database.id,
                product_id=product.id,
                filename=(row.get('filename') or '').strip(),
                file_size=file_size,
                md5=md5,
                sha1=(row.get('sha1') or '').strip().lower() or None,
                sha256=(row.get('sha256') or '').strip().lower() or None,
                crc32=(row.get('crc32') or '').strip().lower() or None,
                is_required=((row.get('is_required') or '1').strip() == '1'),
                relative_path=(row.get('relative_path') or '').strip() or None,
                description=(row.get('description') or '').strip() or None,
            )
            db.session.add(kf)
            files_added += 1

        database.file_count = (database.file_count or 0) + files_added
        db.session.commit()
        flash(f'Imported {files_added} file(s) from CSV into "{database.name}".', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', id=database.id))


# =============================================================================
# Known Products
# =============================================================================

@blueprint.route('/<int:db_id>/products/new', methods=['POST'])
@login_required
@require_permission('read_write')
def new_known_product(db_id):
    HashDatabase.query.get_or_404(db_id)
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', id=db_id))
    product = KnownProduct(
        database_id=db_id,
        title=title,
        description=request.form.get('description', '').strip() or None,
        path_match_enabled='path_match_enabled' in request.form,
    )
    db.session.add(product)
    db.session.commit()
    flash(f'Product "{product.title}" added.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{product.id}')


@blueprint.route('/<int:db_id>/products/<int:pid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', id=db_id))
    product.title = title
    product.description = request.form.get('description', '').strip() or None
    product.path_match_enabled = 'path_match_enabled' in request.form
    db.session.commit()
    flash(f'Product "{product.title}" updated.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')


@blueprint.route('/<int:db_id>/products/<int:pid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = product.title
    db.session.delete(product)
    db.session.commit()
    flash(f'Product "{title}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id))


# =============================================================================
# Known Files (within products)
# =============================================================================

@blueprint.route('/<int:db_id>/products/<int:pid>/files/add', methods=['POST'])
@login_required
@require_permission('read_write')
def add_known_file(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')
    md5 = request.form.get('md5', '').strip().lower() or None
    sha1 = request.form.get('sha1', '').strip().lower() or None
    sha256 = request.form.get('sha256', '').strip().lower() or None
    crc32 = request.form.get('crc32', '').strip().lower() or None
    if not any([md5, sha1, sha256, crc32]):
        flash('At least one hash is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')
    file_size_str = request.form.get('file_size', '').strip()
    file_size = int(file_size_str) if file_size_str.isdigit() else None
    kf = KnownFile(
        database_id=db_id,
        product_id=pid,
        filename=filename,
        file_size=file_size,
        md5=md5,
        sha1=sha1,
        sha256=sha256,
        crc32=crc32,
        is_required='is_required' in request.form,
        relative_path=request.form.get('relative_path', '').strip() or None,
        description=request.form.get('description', '').strip() or None,
    )
    db.session.add(kf)
    product.database.file_count = (product.database.file_count or 0) + 1
    db.session.commit()
    flash(f'File "{filename}" added to "{product.title}".', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')


@blueprint.route('/<int:db_id>/products/<int:pid>/files/<int:fid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_file(db_id, pid, fid):
    kf = KnownFile.query.filter_by(id=fid, product_id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')
    kf.filename = filename
    kf.md5 = request.form.get('md5', '').strip().lower() or None
    kf.sha1 = request.form.get('sha1', '').strip().lower() or None
    kf.sha256 = request.form.get('sha256', '').strip().lower() or None
    kf.crc32 = request.form.get('crc32', '').strip().lower() or None
    file_size_str = request.form.get('file_size', '').strip()
    kf.file_size = int(file_size_str) if file_size_str.isdigit() else None
    kf.is_required = 'is_required' in request.form
    kf.relative_path = request.form.get('relative_path', '').strip() or None
    kf.description = request.form.get('description', '').strip() or None
    db.session.commit()
    flash(f'File "{kf.filename}" updated.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')


@blueprint.route('/<int:db_id>/products/<int:pid>/files/<int:fid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_known_file(db_id, pid, fid):
    kf = KnownFile.query.filter_by(id=fid, product_id=pid, database_id=db_id).first_or_404()
    filename = kf.filename
    database = kf.database
    db.session.delete(kf)
    if database.file_count and database.file_count > 0:
        database.file_count -= 1
    db.session.commit()
    flash(f'File "{filename}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=db_id) + f'#product-{pid}')


# vim: ts=4 sw=4 et
