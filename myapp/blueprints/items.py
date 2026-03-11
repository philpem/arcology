"""
Arcology - Items Blueprint

CRUD operations for collection items.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Optional, Length
from sqlalchemy import or_

from ..extensions import db
from ..database import Item, Platform, Category, Tag, ExternalSystem, ExternalReference
from .artefacts import _delete_item_files
from ..permissions import require_permission
from ..utils.slugs import get_or_create_slug, lookup_by_identifier

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/items', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Items", f"{ROUTENAME}.index", 100)


# =============================================================================
# Forms
# =============================================================================

class ItemForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=255)])
    description = TextAreaField('Description', validators=[Optional()])
    platform_id = SelectField('Platform', coerce=int, validators=[Optional()])
    category_id = SelectField('Category', coerce=int, validators=[Optional()])
    tags = StringField('Tags', validators=[Optional()],
                       description='Comma-separated list of tags')


class ExternalReferenceForm(FlaskForm):
    system_id = SelectField('External System', coerce=int, validators=[DataRequired()])
    external_id = StringField('External ID', validators=[DataRequired()],
                              description='The ID/reference in the external system')
    external_url = StringField('Direct URL', validators=[Optional()],
                               description='Optional: override the generated URL')
    notes = TextAreaField('Notes', validators=[Optional()])


class SearchForm(FlaskForm):
    q = StringField('Search', validators=[Optional()])
    platform_id = SelectField('Platform', coerce=int, validators=[Optional()])
    category_id = SelectField('Category', coerce=int, validators=[Optional()])


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    """List all items with search/filter."""
    form = SearchForm(request.args)
    
    form.platform_id.choices = [(0, '-- All Platforms --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]
    form.category_id.choices = [(0, '-- All Categories --')] + [
        (c.id, c.name) for c in Category.query.order_by(Category.name).all()
    ]
    
    query = Item.query
    
    if form.q.data:
        search = f'%{form.q.data}%'
        query = query.filter(or_(
            Item.name.ilike(search),
            Item.description.ilike(search)
        ))
    
    if form.platform_id.data and form.platform_id.data != 0:
        query = query.filter(Item.platform_id == form.platform_id.data)
    
    if form.category_id.data and form.category_id.data != 0:
        query = query.filter(Item.category_id == form.category_id.data)
    
    page = request.args.get('page', 1, type=int)
    per_page = current_app.config.get('ITEMS_PER_PAGE', 25)
    pagination = query.order_by(Item.name).paginate(page=page, per_page=per_page)
    
    return render_template('items/index.html',
                           items=pagination.items,
                           pagination=pagination,
                           form=form)


@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new():
    """Create a new item."""
    form = ItemForm()
    
    form.platform_id.choices = [(0, '-- Select Platform --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]
    form.category_id.choices = [(0, '-- Select Category --')] + [
        (c.id, c.name) for c in Category.query.order_by(Category.name).all()
    ]
    
    if form.validate_on_submit():
        item = Item(
            name=form.name.data,
            description=form.description.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            category_id=form.category_id.data if form.category_id.data != 0 else None
        )
        
        if form.tags.data:
            tag_names = [t.strip() for t in form.tags.data.split(',') if t.strip()]
            for tag_name in tag_names:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                item.tags.append(tag)
        
        db.session.add(item)
        db.session.commit()
        get_or_create_slug(item, 'name')

        flash(f'Item "{item.name}" created successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return render_template('items/form.html', form=form, title='New Item')


@blueprint.route('/<string:uuid>')
@login_required
def view(uuid):
    """View an item and its artefacts."""
    item = lookup_by_identifier(Item, uuid)
    return render_template('items/view.html', item=item)


@blueprint.route('/<string:uuid>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit(uuid):
    """Edit an item."""
    item = lookup_by_identifier(Item, uuid)
    form = ItemForm(obj=item)
    
    form.platform_id.choices = [(0, '-- Select Platform --')] + [
        (p.id, p.name) for p in Platform.query.order_by(Platform.name).all()
    ]
    form.category_id.choices = [(0, '-- Select Category --')] + [
        (c.id, c.name) for c in Category.query.order_by(Category.name).all()
    ]
    
    if request.method == 'GET':
        form.tags.data = ', '.join([t.name for t in item.tags])
    
    if form.validate_on_submit():
        item.name = form.name.data
        item.description = form.description.data
        item.platform_id = form.platform_id.data if form.platform_id.data != 0 else None
        item.category_id = form.category_id.data if form.category_id.data != 0 else None
        
        item.tags.clear()
        if form.tags.data:
            tag_names = [t.strip() for t in form.tags.data.split(',') if t.strip()]
            for tag_name in tag_names:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                item.tags.append(tag)
        
        db.session.commit()

        flash(f'Item "{item.name}" updated successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return render_template('items/form.html', form=form, item=item, title='Edit Item')


@blueprint.route('/<string:uuid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete(uuid):
    """Delete an item."""
    item = lookup_by_identifier(Item, uuid)
    name = item.name

    # Delete all files on disk before the cascade removes DB records.
    _delete_item_files(item)

    db.session.delete(item)
    db.session.commit()

    flash(f'Item "{name}" deleted.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<string:uuid>/references/add', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def add_reference(uuid):
    """Add an external reference to an item."""
    item = lookup_by_identifier(Item, uuid)
    form = ExternalReferenceForm()
    
    form.system_id.choices = [
        (s.id, s.name) for s in ExternalSystem.query.order_by(ExternalSystem.name).all()
    ]
    
    if not form.system_id.choices:
        flash('No external systems configured. Please add one first.', 'warning')
        return redirect(url_for('myapp_blueprints_taxonomy.external_systems'))
    
    if form.validate_on_submit():
        ref = ExternalReference(
            item_id=item.id,
            system_id=form.system_id.data,
            external_id=form.external_id.data,
            external_url=form.external_url.data or None,
            notes=form.notes.data
        )
        
        db.session.add(ref)
        db.session.commit()

        flash('External reference added.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return render_template('items/add_reference.html', form=form, item=item)


@blueprint.route('/<string:item_uuid>/references/<int:ref_id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_reference(item_uuid, ref_id):
    """Delete an external reference."""
    item = lookup_by_identifier(Item, item_uuid)
    ref = ExternalReference.query.get_or_404(ref_id)

    if ref.item_id != item.id:
        flash('Invalid reference.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    db.session.delete(ref)
    db.session.commit()

    flash('External reference removed.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))


# vim: ts=4 sw=4 et
