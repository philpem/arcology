"""
Arcology - Items Blueprint

CRUD operations for collection items.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Optional, Length
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..database import Item, Artefact, Platform, Category, ExternalSystem, ExternalReference

_ITEM_SORT_OPTIONS = {
    'name_asc':      func.lower(Item.name).asc(),
    'name_desc':     func.lower(Item.name).desc(),
    'uploaded_asc':  Item.created_at.asc(),
    'uploaded_desc': Item.created_at.desc(),
}

_ARTEFACT_SORT_OPTIONS = {
    'label_asc':     func.lower(Artefact.label).asc(),
    'label_desc':    func.lower(Artefact.label).desc(),
    'uploaded_asc':  Artefact.created_at.asc(),
    'uploaded_desc': Artefact.created_at.desc(),
}
from .artefacts import bulk_delete_item
from ..permissions import require_permission
from ..utils.item_helpers import item_choice_list, item_parent_choice_list, assign_item_fields, assign_item_tags

from ..utils.slugs import get_or_create_slug, lookup_by_identifier
from ..utils.pagination import compute_letter_pages, resolve_per_page, resolve_sort, VALID_PER_PAGE

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
    parent_id = SelectField('Parent Item', coerce=int, validators=[Optional()])
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

    form.platform_id.choices = item_choice_list(Platform, '-- All Platforms --')
    form.category_id.choices = item_choice_list(Category, '-- All Categories --')

    # Tree view toggle: 'tree' shows indented hierarchy; default shows root items only.
    view_mode = request.args.get('view', 'flat')
    searching = bool(form.q.data or
                     (form.platform_id.data and form.platform_id.data != 0) or
                     (form.category_id.data and form.category_id.data != 0))

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

    # In flat and tree modes (not searching), paginate only root items;
    # tree mode recursively expands children via _build_tree_rows.
    if view_mode in ('flat', 'tree') and not searching:
        query = query.filter(Item.parent_id.is_(None))

    # Eager-load platform, category, and children to avoid N+1 lazy loads in template
    query = query.options(
        selectinload(Item.platform),
        selectinload(Item.category),
        selectinload(Item.children),
    )

    sort = resolve_sort('sort', _ITEM_SORT_OPTIONS, 'items_sort', 'name_asc')
    per_page, page, view_all = resolve_per_page('ITEMS_PER_PAGE', 25)

    # Compute letter-to-page mapping for A-Z jump bar (only meaningful for name sorts)
    if sort in ('name_asc', 'name_desc'):
        letter_pages, current_letter = compute_letter_pages(
            query, Item.name, per_page, current_page=page, descending=(sort == 'name_desc')
        )
    else:
        letter_pages, current_letter = {}, None

    pagination = query.order_by(_ITEM_SORT_OPTIONS[sort]).paginate(page=page, per_page=per_page)

    # Build tree rows first (if tree mode) so we know all visible item IDs
    tree_rows = None
    if view_mode == 'tree' and not searching:
        tree_rows = _build_tree_rows(pagination.items)

    # Collect all visible item IDs — in tree mode this includes expanded children
    if tree_rows:
        item_ids = [item.id for item, _depth in tree_rows]
    else:
        item_ids = [item.id for item in pagination.items]

    # Compute artefact and child counts in batch queries
    artefact_counts = {}
    child_counts = {}
    if item_ids:
        counts = (
            db.session.query(Artefact.item_id, func.count(Artefact.id))
            .filter(Artefact.item_id.in_(item_ids))
            .group_by(Artefact.item_id)
            .all()
        )
        artefact_counts = dict(counts)
        child_counts_q = (
            db.session.query(Item.parent_id, func.count(Item.id))
            .filter(Item.parent_id.in_(item_ids))
            .group_by(Item.parent_id)
            .all()
        )
        child_counts = dict(child_counts_q)

    return render_template('items/index.html',
                           items=pagination.items,
                           artefact_counts=artefact_counts,
                           child_counts=child_counts,
                           tree_rows=tree_rows,
                           pagination=pagination,
                           form=form,
                           letter_pages=letter_pages,
                           current_letter=current_letter,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all,
                           sort=sort,
                           view_mode=view_mode,
                           searching=searching)


def _build_tree_rows(root_items):
    """Recursively expand root items into (item, depth) rows for tree display."""
    rows = []

    def _recurse(item, depth):
        rows.append((item, depth))
        for child in sorted(item.children, key=lambda c: c.name):
            _recurse(child, depth + 1)

    for item in root_items:
        _recurse(item, 0)
    return rows


@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new():
    """Create a new item."""
    form = ItemForm()

    form.platform_id.choices = item_choice_list(Platform, '-- Select Platform --')
    form.category_id.choices = item_choice_list(Category, '-- Select Category --')
    form.parent_id.choices = item_parent_choice_list('-- No parent (root item) --')

    # Pre-select parent if ?parent=<uuid> is provided (e.g. from "New Child Item" button)
    preset_parent = None
    if request.method == 'GET':
        parent_uuid = request.args.get('parent')
        if parent_uuid:
            preset_parent = Item.query.filter(
                (Item.uuid == parent_uuid) | (Item.uuid.like(f'{parent_uuid}%'))
            ).first()
            if preset_parent:
                form.parent_id.data = preset_parent.id

    if form.validate_on_submit():
        item = Item()
        assign_item_fields(
            item,
            name=form.name.data,
            description=form.description.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            category_id=form.category_id.data if form.category_id.data != 0 else None,
            parent_id=form.parent_id.data if form.parent_id.data != 0 else None,
        )
        assign_item_tags(item, form.tags.data)

        db.session.add(item)
        db.session.commit()
        get_or_create_slug(item, 'name')

        flash(f'Item "{item.name}" created successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return render_template('items/form.html', form=form, title='New Item', preset_parent=preset_parent)


@blueprint.route('/<string:uuid>')
@login_required
def view(uuid):
    """View an item and its artefacts."""
    item = lookup_by_identifier(Item, uuid)

    per_page, page, view_all = resolve_per_page('ARTEFACTS_PER_PAGE', 25)

    artefact_sort = resolve_sort('artefact_sort', _ARTEFACT_SORT_OPTIONS, 'artefacts_sort', 'label_asc')

    artefact_query = (
        Artefact.query
        .filter_by(item_id=item.id, parent_artefact_id=None)
        .options(selectinload(Artefact.derived_artefacts))
    )

    # Compute letter-to-page mapping for A-Z jump bar (only meaningful for label sorts)
    if artefact_sort in ('label_asc', 'label_desc'):
        letter_pages, current_letter = compute_letter_pages(
            artefact_query, Artefact.label, per_page, current_page=page,
            descending=(artefact_sort == 'label_desc')
        )
    else:
        letter_pages, current_letter = {}, None

    artefacts_page = artefact_query.order_by(_ARTEFACT_SORT_OPTIONS[artefact_sort]).paginate(page=page, per_page=per_page)

    return render_template('items/view.html', item=item, artefacts_page=artefacts_page,
                           letter_pages=letter_pages, current_letter=current_letter,
                           valid_per_page=VALID_PER_PAGE, view_all=view_all,
                           artefact_sort=artefact_sort)


@blueprint.route('/<string:uuid>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def edit(uuid):
    """Edit an item (including moving it to a different parent)."""
    item = lookup_by_identifier(Item, uuid)
    form = ItemForm(obj=item)

    form.platform_id.choices = item_choice_list(Platform, '-- Select Platform --')
    form.category_id.choices = item_choice_list(Category, '-- Select Category --')
    # Exclude self and descendants from the parent dropdown to prevent cycles
    form.parent_id.choices = item_parent_choice_list('-- No parent (root item) --', exclude_item=item)

    if request.method == 'GET':
        form.tags.data = ', '.join([t.name for t in item.tags])
        form.parent_id.data = item.parent_id or 0

    if form.validate_on_submit():
        new_parent_id = form.parent_id.data if form.parent_id.data != 0 else None

        # Cycle prevention: ensure the chosen parent is not a descendant of this item
        if new_parent_id is not None:
            new_parent = Item.query.get(new_parent_id)
            if new_parent and item.is_ancestor_of(new_parent):
                flash('Cannot move an item to one of its own descendants.', 'danger')
                return render_template('items/form.html', form=form, item=item, title='Edit Item', preset_parent=None)

        assign_item_fields(
            item,
            name=form.name.data,
            description=form.description.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            category_id=form.category_id.data if form.category_id.data != 0 else None,
            parent_id=new_parent_id,
        )
        assign_item_tags(item, form.tags.data)

        db.session.commit()

        flash(f'Item "{item.name}" updated successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return render_template('items/form.html', form=form, item=item, title='Edit Item', preset_parent=None)


@blueprint.route('/<string:uuid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete(uuid):
    """Delete an item and all its descendants (cascade)."""
    item = lookup_by_identifier(Item, uuid)
    name = item.name
    parent = item.parent

    bulk_delete_item(item)

    flash(f'Item "{name}" deleted.', 'success')
    # Redirect to parent if we came from within the hierarchy
    if parent:
        return redirect(url_for(f'{ROUTENAME}.view', uuid=parent.url_id))
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
