"""
Arcology - Taxonomy Blueprint

Platforms, categories, tags, and external systems.
"""

from flask import Blueprint, render_template, flash, request
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Optional, Length

from sqlalchemy import func

from ..extensions import db
from ..database import Platform, Category, Tag, ExternalSystem, HashDatabase
from ..permissions import require_permission
from ..utils.web_forms import redirect_local
from ..utils.db_helpers import model_choice_list

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


def _route_redirect(endpoint: str, **values):
    """Redirect to a taxonomy endpoint by local route name."""
    return redirect_local(ROUTENAME, endpoint, **values)


def _save_named_description_model(obj, form, *, parent_field: bool = False):
    """Copy the common name/description[/parent] form fields onto an ORM object."""
    obj.name = form.name.data
    obj.description = form.description.data
    if parent_field:
        obj.parent_id = form.parent_id.data if form.parent_id.data != 0 else None


def _save_external_system(obj, form):
    """Copy ExternalSystem form fields onto an ORM object."""
    obj.name = form.name.data
    obj.system_type = form.system_type.data
    obj.base_url = form.base_url.data
    obj.url_template = form.url_template.data
    obj.description = form.description.data


def _delete_with_guards(obj, endpoint: str, success_label: str, guards: list[tuple[bool, str]]):
    """Delete an object after evaluating guard conditions.

    Returns a redirect response in all cases.
    """
    for condition, message in guards:
        if condition:
            flash(message, 'error')
            return _route_redirect(endpoint)

    name = obj.name
    db.session.delete(obj)
    db.session.commit()
    flash(f'{success_label} "{name}" deleted.', 'success')
    return _route_redirect(endpoint)


# =============================================================================
# Platforms
# =============================================================================

@blueprint.route('/platforms')
@login_required
def platforms():
    platforms = Platform.query.filter(Platform.parent_id.is_(None)).order_by(func.lower(Platform.name)).all()
    return render_template('taxonomy/platforms.html', platforms=platforms)


@blueprint.route('/platforms/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_platform():
    form = PlatformForm()
    form.parent_id.choices = model_choice_list(Platform, label='-- No Parent --')
    
    if form.validate_on_submit():
        platform = Platform()
        _save_named_description_model(platform, form, parent_field=True)
        db.session.add(platform)
        db.session.commit()
        flash(f'Platform "{platform.name}" created.', 'success')
        return _route_redirect('platforms')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Platform')


@blueprint.route('/platforms/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_platform(id):
    platform = Platform.query.get_or_404(id)
    form = PlatformForm(obj=platform)

    exclude_ids = {platform.id} | _collect_descendant_ids(platform)
    form.parent_id.choices = model_choice_list(Platform, label='-- No Parent --', exclude_ids=exclude_ids)

    if form.validate_on_submit():
        _save_named_description_model(platform, form, parent_field=True)
        db.session.commit()
        flash(f'Platform "{platform.name}" updated.', 'success')
        return _route_redirect('platforms')

    return render_template('taxonomy/taxonomy_form.html', form=form, platform=platform, title='Edit Platform')


@blueprint.route('/platforms/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_platform(id):
    platform = Platform.query.get_or_404(id)
    return _delete_with_guards(platform, 'platforms', 'Platform', [
        (bool(platform.items), 'Cannot delete platform with associated items.'),
        (bool(platform.children), 'Cannot delete platform with child platforms.'),
        (bool(HashDatabase.query.filter_by(platform_id=platform.id).first()),
         'Cannot delete platform with associated hash databases.'),
    ])


# =============================================================================
# Categories
# =============================================================================

@blueprint.route('/categories')
@login_required
def categories():
    categories = Category.query.filter(Category.parent_id.is_(None)).order_by(func.lower(Category.name)).all()
    return render_template('taxonomy/categories.html', categories=categories)


@blueprint.route('/categories/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_category():
    form = CategoryForm()
    form.parent_id.choices = model_choice_list(Category, label='-- No Parent --')
    
    if form.validate_on_submit():
        category = Category()
        _save_named_description_model(category, form, parent_field=True)
        db.session.add(category)
        db.session.commit()
        flash(f'Category "{category.name}" created.', 'success')
        return _route_redirect('categories')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Category')


@blueprint.route('/categories/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_category(id):
    category = Category.query.get_or_404(id)
    form = CategoryForm(obj=category)
    
    exclude_ids = {category.id} | _collect_descendant_ids(category)
    form.parent_id.choices = model_choice_list(Category, label='-- No Parent --', exclude_ids=exclude_ids)
    
    if form.validate_on_submit():
        _save_named_description_model(category, form, parent_field=True)
        db.session.commit()
        flash(f'Category "{category.name}" updated.', 'success')
        return _route_redirect('categories')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, category=category, title='Edit Category')


@blueprint.route('/categories/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_category(id):
    category = Category.query.get_or_404(id)
    return _delete_with_guards(category, 'categories', 'Category', [
        (bool(category.items), 'Cannot delete category with associated items.'),
        (bool(category.children), 'Cannot delete category with child categories.'),
    ])


# =============================================================================
# Tags
# =============================================================================

@blueprint.route('/tags')
@login_required
def tags():
    tags = Tag.query.order_by(func.lower(Tag.name)).all()
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
        return _route_redirect('tags')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New Tag')


@blueprint.route('/tags/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_tag(id):
    tag = Tag.query.get_or_404(id)
    return _delete_with_guards(tag, 'tags', 'Tag', [])


# =============================================================================
# External Systems
# =============================================================================

@blueprint.route('/external-systems')
@login_required
def external_systems():
    systems = ExternalSystem.query.order_by(func.lower(ExternalSystem.name)).all()
    return render_template('taxonomy/external_systems.html', systems=systems)


@blueprint.route('/external-systems/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new_external_system():
    form = ExternalSystemForm()
    
    if form.validate_on_submit():
        system = ExternalSystem()
        _save_external_system(system, form)
        db.session.add(system)
        db.session.commit()
        flash(f'External system "{system.name}" created.', 'success')
        return _route_redirect('external_systems')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, title='New External System')


@blueprint.route('/external-systems/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit_external_system(id):
    system = ExternalSystem.query.get_or_404(id)
    form = ExternalSystemForm(obj=system)
    
    if form.validate_on_submit():
        _save_external_system(system, form)
        db.session.commit()
        flash(f'External system "{system.name}" updated.', 'success')
        return _route_redirect('external_systems')
    
    return render_template('taxonomy/taxonomy_form.html', form=form, system=system, title='Edit External System')


@blueprint.route('/external-systems/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_external_system(id):
    system = ExternalSystem.query.get_or_404(id)
    return _delete_with_guards(system, 'external_systems', 'External system', [
        (bool(system.references), 'Cannot delete external system with associated references.'),
    ])


# vim: ts=4 sw=4 et
