"""
Arcology - Taxonomy Blueprint

Platforms, categories, tags, and external systems.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Optional, Length

from ..extensions import db
from ..database import Platform, Category, Tag, ExternalSystem, HashDatabase
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
    
    return render_template('taxonomy/platform_form.html', form=form, title='New Platform')


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
    
    return render_template('taxonomy/platform_form.html', form=form, platform=platform, title='Edit Platform')


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

    if HashDatabase.query.filter_by(platform_id=platform.id).first():
        flash('Cannot delete platform with associated hash databases.', 'error')
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
    
    return render_template('taxonomy/category_form.html', form=form, title='New Category')


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
    
    return render_template('taxonomy/category_form.html', form=form, category=category, title='Edit Category')


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
    
    return render_template('taxonomy/tag_form.html', form=form, title='New Tag')


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
    
    return render_template('taxonomy/external_system_form.html', form=form, title='New External System')


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
    
    return render_template('taxonomy/external_system_form.html', form=form, system=system, title='Edit External System')


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


# vim: ts=4 sw=4 et
