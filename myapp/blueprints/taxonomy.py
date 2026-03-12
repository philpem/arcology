"""
Arcology - Taxonomy Blueprint

Platforms, categories, tags, external systems, and hash databases.
"""

import csv
import io
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, jsonify
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional, URL, Length

from ..extensions import db
from ..database import Platform, Category, Tag, ExternalSystem, HashDatabase, KnownProduct, KnownFile
from ..permissions import require_permission

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/taxonomy', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Taxonomy", f"{ROUTENAME}.index", 200)


# =============================================================================
# Index / Navigation
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    """Taxonomy overview - redirect to platforms or show navigation."""
    return render_template('taxonomy/index.html')


# =============================================================================
# Forms
# =============================================================================

class PlatformForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    parent_id = SelectField('Parent Platform', coerce=int, validators=[Optional()])


class CategoryForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    parent_id = SelectField('Parent Category', coerce=int, validators=[Optional()])


class TagForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=50)])


class ExternalSystemForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    system_type = StringField('System Type', validators=[Optional()],
                              description='e.g., collection_management, accession_register')
    base_url = StringField('Base URL', validators=[Optional()])
    url_template = StringField('URL Template', validators=[Optional()],
                               description='e.g., /items/{id}')
    description = TextAreaField('Description', validators=[Optional()])


def _collect_descendant_ids(node):
    """Return the set of IDs of all descendants of node (recursive)."""
    ids = set()
    for child in node.children:
        ids.add(child.id)
        ids |= _collect_descendant_ids(child)
    return ids


class HashDatabaseForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    source_url = StringField('Source URL', validators=[Optional()])
    version = StringField('Version', validators=[Optional(), Length(max=50)])
    platform_id = SelectField('Platform', coerce=int, validators=[Optional()])
    enable_product_recognition = BooleanField('Enable product/application recognition')


# =============================================================================
# Platforms
# =============================================================================

@blueprint.route('/platforms')
@login_required
def platforms():
    platforms = Platform.query.filter(Platform.parent_id.is_(None)).order_by(Platform.name).all()
    return render_template('taxonomy/platforms.html', platforms=platforms)


@blueprint.route('/platforms/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_platform():
    form = PlatformForm()
    form.parent_id.choices = [(0, '-- No Parent --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]
    
    if form.validate_on_submit():
        platform = Platform(
            name=form.name.data,
            description=form.description.data,
            parent_id=form.parent_id.data if form.parent_id.data != 0 else None
        )
        db.session.add(platform)
        db.session.commit()
        flash(f'Platform "{platform.name}" created.', 'success')
        return redirect(url_for(f'{ROUTENAME}.platforms'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Platform')


@blueprint.route('/platforms/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_platform(id):
    platform = Platform.query.get_or_404(id)
    form = PlatformForm(obj=platform)
    
    exclude_ids = {platform.id} | _collect_descendant_ids(platform)
    form.parent_id.choices = [(0, '-- No Parent --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
        if p.id not in exclude_ids
    ]
    
    if form.validate_on_submit():
        platform.name = form.name.data
        platform.description = form.description.data
        platform.parent_id = form.parent_id.data if form.parent_id.data != 0 else None
        db.session.commit()
        flash(f'Platform "{platform.name}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.platforms'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, platform=platform, title='Edit Platform')


@blueprint.route('/platforms/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_platform(id):
    platform = Platform.query.get_or_404(id)
    
    if platform.items:
        flash('Cannot delete platform with associated items.', 'error')
        return redirect(url_for(f'{ROUTENAME}.platforms'))
    
    if platform.children:
        flash('Cannot delete platform with child platforms.', 'error')
        return redirect(url_for(f'{ROUTENAME}.platforms'))
    
    name = platform.name
    db.session.delete(platform)
    db.session.commit()
    flash(f'Platform "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.platforms'))


# =============================================================================
# Categories
# =============================================================================

@blueprint.route('/categories')
@login_required
def categories():
    categories = Category.query.filter(Category.parent_id.is_(None)).order_by(Category.name).all()
    return render_template('taxonomy/categories.html', categories=categories)


@blueprint.route('/categories/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_category():
    form = CategoryForm()
    form.parent_id.choices = [(0, '-- No Parent --')] + [
        (c.id, c.name) for c in Category.query.order_by(Category.name).all()
    ]
    
    if form.validate_on_submit():
        category = Category(
            name=form.name.data,
            description=form.description.data,
            parent_id=form.parent_id.data if form.parent_id.data != 0 else None
        )
        db.session.add(category)
        db.session.commit()
        flash(f'Category "{category.name}" created.', 'success')
        return redirect(url_for(f'{ROUTENAME}.categories'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Category')


@blueprint.route('/categories/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_category(id):
    category = Category.query.get_or_404(id)
    form = CategoryForm(obj=category)
    
    exclude_ids = {category.id} | _collect_descendant_ids(category)
    form.parent_id.choices = [(0, '-- No Parent --')] + [
        (c.id, c.name) for c in Category.query.order_by(Category.name).all()
        if c.id not in exclude_ids
    ]
    
    if form.validate_on_submit():
        category.name = form.name.data
        category.description = form.description.data
        category.parent_id = form.parent_id.data if form.parent_id.data != 0 else None
        db.session.commit()
        flash(f'Category "{category.name}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.categories'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, category=category, title='Edit Category')


@blueprint.route('/categories/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_category(id):
    category = Category.query.get_or_404(id)
    
    if category.items:
        flash('Cannot delete category with associated items.', 'error')
        return redirect(url_for(f'{ROUTENAME}.categories'))
    
    if category.children:
        flash('Cannot delete category with child categories.', 'error')
        return redirect(url_for(f'{ROUTENAME}.categories'))
    
    name = category.name
    db.session.delete(category)
    db.session.commit()
    flash(f'Category "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.categories'))


# =============================================================================
# Tags
# =============================================================================

@blueprint.route('/tags')
@login_required
def tags():
    tags = Tag.query.order_by(Tag.name).all()
    return render_template('taxonomy/tags.html', tags=tags)


@blueprint.route('/tags/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_tag():
    form = TagForm()
    
    if form.validate_on_submit():
        tag = Tag(name=form.name.data)
        db.session.add(tag)
        db.session.commit()
        flash(f'Tag "{tag.name}" created.', 'success')
        return redirect(url_for(f'{ROUTENAME}.tags'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Tag')


@blueprint.route('/tags/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_tag(id):
    tag = Tag.query.get_or_404(id)
    form = TagForm(obj=tag)

    if form.validate_on_submit():
        tag.name = form.name.data
        db.session.commit()
        flash(f'Tag "{tag.name}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.tags'))

    return render_template('taxonomy/taxonomy_form.html', form=form, title='Edit Tag')


@blueprint.route('/tags/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_tag(id):
    tag = Tag.query.get_or_404(id)
    name = tag.name
    db.session.delete(tag)
    db.session.commit()
    flash(f'Tag "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.tags'))


# =============================================================================
# External Systems
# =============================================================================

@blueprint.route('/external-systems')
@login_required
def external_systems():
    systems = ExternalSystem.query.order_by(ExternalSystem.name).all()
    return render_template('taxonomy/external_systems.html', systems=systems)


@blueprint.route('/external-systems/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_external_system():
    form = ExternalSystemForm()
    
    if form.validate_on_submit():
        system = ExternalSystem(
            name=form.name.data,
            system_type=form.system_type.data,
            base_url=form.base_url.data,
            url_template=form.url_template.data,
            description=form.description.data
        )
        db.session.add(system)
        db.session.commit()
        flash(f'External system "{system.name}" created.', 'success')
        return redirect(url_for(f'{ROUTENAME}.external_systems'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New External System')


@blueprint.route('/external-systems/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_external_system(id):
    system = ExternalSystem.query.get_or_404(id)
    form = ExternalSystemForm(obj=system)
    
    if form.validate_on_submit():
        system.name = form.name.data
        system.system_type = form.system_type.data
        system.base_url = form.base_url.data
        system.url_template = form.url_template.data
        system.description = form.description.data
        db.session.commit()
        flash(f'External system "{system.name}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.external_systems'))
    
    return render_template('taxonomy/taxonomy_form.html', form=form, system=system, title='Edit External System')


@blueprint.route('/external-systems/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_external_system(id):
    system = ExternalSystem.query.get_or_404(id)
    
    if system.references:
        flash('Cannot delete external system with associated references.', 'error')
        return redirect(url_for(f'{ROUTENAME}.external_systems'))
    
    name = system.name
    db.session.delete(system)
    db.session.commit()
    flash(f'External system "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.external_systems'))


# =============================================================================
# Hash Databases
# =============================================================================

@blueprint.route('/hash-databases')
@login_required
def hash_databases():
    databases = HashDatabase.query.order_by(HashDatabase.name).all()
    return render_template('taxonomy/hash_databases.html', databases=databases)


def _platform_choices():
    return [(0, '-- All Platforms --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]


@blueprint.route('/hash-databases/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_hash_database():
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
        return redirect(url_for(f'{ROUTENAME}.hash_databases'))

    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Hash Database')


@blueprint.route('/hash-databases/<int:id>')
@login_required
def view_hash_database(id):
    database = HashDatabase.query.get_or_404(id)
    products = KnownProduct.query.filter_by(database_id=id).order_by(KnownProduct.title).all()
    platforms = Platform.query.order_by(Platform.name).all()
    return render_template('taxonomy/hash_database_view.html',
                           database=database,
                           products=products,
                           platforms=platforms)


@blueprint.route('/hash-databases/<int:id>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_hash_database(id):
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
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=id))


@blueprint.route('/hash-databases/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_hash_database(id):
    database = HashDatabase.query.get_or_404(id)
    form = HashDatabaseForm(obj=database)
    form.platform_id.choices = [(0, '-- All Platforms --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]

    if form.validate_on_submit():
        database.name = form.name.data
        database.description = form.description.data
        database.source_url = form.source_url.data
        database.version = form.version.data
        database.platform_id = form.platform_id.data if form.platform_id.data != 0 else None
        db.session.commit()
        flash(f'Hash database "{database.name}" updated.', 'success')
        return redirect(url_for(f'{ROUTENAME}.hash_databases'))

    # Pre-select current platform in the dropdown
    if database.platform_id:
        form.platform_id.data = database.platform_id
    return render_template('taxonomy/taxonomy_form.html', form=form, title='Edit Hash Database')


@blueprint.route('/hash-databases/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_hash_database(id):
    database = HashDatabase.query.get_or_404(id)
    name = database.name
    db.session.delete(database)
    db.session.commit()
    flash(f'Hash database "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.hash_databases'))


@blueprint.route('/hash-databases/<int:id>/export')
@login_required
def export_hash_database(id):
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
        # Also write uncategorised files (product_id is null)
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

    # JSON export
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
# Known Products
# =============================================================================

@blueprint.route('/hash-databases/<int:db_id>/products/new', methods=['POST'])
@login_required
@require_permission('read_write')
def new_known_product(db_id):
    database = HashDatabase.query.get_or_404(db_id)
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id))
    product = KnownProduct(
        database_id=db_id,
        title=title,
        description=request.form.get('description', '').strip() or None,
        path_match_enabled='path_match_enabled' in request.form,
    )
    db.session.add(product)
    db.session.commit()
    flash(f'Product "{product.title}" added.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{product.id}')


@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id))
    product.title = title
    product.description = request.form.get('description', '').strip() or None
    product.path_match_enabled = 'path_match_enabled' in request.form
    db.session.commit()
    flash(f'Product "{product.title}" updated.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')


@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = product.title
    db.session.delete(product)
    db.session.commit()
    flash(f'Product "{title}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id))


# =============================================================================
# Known Files (within products)
# =============================================================================

@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/files/add', methods=['POST'])
@login_required
@require_permission('read_write')
def add_known_file(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')
    md5 = request.form.get('md5', '').strip().lower() or None
    sha1 = request.form.get('sha1', '').strip().lower() or None
    sha256 = request.form.get('sha256', '').strip().lower() or None
    crc32 = request.form.get('crc32', '').strip().lower() or None
    if not any([md5, sha1, sha256, crc32]):
        flash('At least one hash is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')
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
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')


@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/files/<int:fid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_file(db_id, pid, fid):
    kf = KnownFile.query.filter_by(id=fid, product_id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')
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
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')


@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/files/<int:fid>/delete', methods=['POST'])
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
    return redirect(url_for(f'{ROUTENAME}.view_hash_database', id=db_id) + f'#product-{pid}')


# vim: ts=4 sw=4 et
