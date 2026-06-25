"""
Arcology - Items Blueprint

CRUD operations for collection items.
"""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional
from ..database import (
    Analysis,
    AnalysisStatus,
    Artefact,
    Category,
    ExternalReference,
    ExternalSystem,
    Group,
    Item,
    ItemShare,
    Platform,
    Tag,
    User,
    UserPermission,
)
from ..extensions import db
from ..permissions import public_readable, require_permission, require_visible_item
from ..services.artefact_lifecycle import (
    mark_item_pending_deletion,
    queue_item_delete,
)
from ..utils.item_helpers import (
    assign_item_fields,
    assign_item_tags,
    indented_item_choices,
    indented_taxonomy_choices,
    item_parent_choice_list,
)
from ..utils.pagination import VALID_PER_PAGE, compute_letter_pages, resolve_per_page, resolve_sort
from ..utils.privacy import recompute_item_privacy
from ..utils.slugs import ensure_unique_slug, generate_slug, get_or_create_slug
from ..visibility import (
    SHARE_PERMISSIONS,
    artefact_visibility_clause,
    can_change_owner,
    can_claim_item,
    can_contribute_to_item,
    can_curate_item,
    can_manage_privacy,
    can_manage_shares,
    can_view_item,
    item_visibility_clause,
)
from .analysis import REPRIORITISE_CHOICES

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
    is_private = BooleanField('Private',
                              description='Visible only to you and administrators. '
                                          'Privacy descends to all sub-items and artefacts.')
    owner_id = SelectField('Owner', coerce=int, validators=[Optional()],
                           description='Reassign this item to another user.')


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
@public_readable
def index():
    """List all items with search/filter."""
    form = SearchForm(request.args)

    form.platform_id.choices = indented_taxonomy_choices(Platform, '-- All Platforms --')
    form.category_id.choices = indented_taxonomy_choices(Category, '-- All Categories --')

    # Tree view toggle: 'tree' shows indented hierarchy; default shows root items only.
    view_mode = request.args.get('view', 'flat')
    searching = bool(form.q.data or
                     (form.platform_id.data and form.platform_id.data != 0) or
                     (form.category_id.data and form.category_id.data != 0))

    query = Item.query.filter(item_visibility_clause(current_user))

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
        tree_rows = [(it, depth) for it, depth in tree_rows
                     if can_view_item(it, current_user)]

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
            .join(Item, Artefact.item_id == Item.id)
            .filter(Artefact.item_id.in_(item_ids))
            .filter(artefact_visibility_clause(current_user))
            .group_by(Artefact.item_id)
            .all()
        )
        artefact_counts = dict(counts)
        child_counts_q = (
            db.session.query(Item.parent_id, func.count(Item.id))
            .filter(Item.parent_id.in_(item_ids))
            .filter(item_visibility_clause(current_user))
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


def _compute_artefact_analysis_status(root_artefact_ids):
    """Aggregate analysis statuses across each root artefact's derived tree.

    For each root artefact, sums analysis statuses across the artefact itself
    and every descendant (via ``parent_artefact_id``). Returns a mapping
    ``{root_artefact_id: (status_value, count, total)}``. Priority order
    (worst-first): RUNNING > FAILED > PENDING > COMPLETED. Roots with no
    analyses anywhere in their tree are absent from the dict.
    """
    if not root_artefact_ids:
        return {}

    # Walk the derived-artefact tree by iterative BFS, building a map from
    # every descendant artefact_id back to the root it belongs to. Depth is
    # typically 2-3 levels in practice, so this is a small number of queries.
    descendant_to_root = {aid: aid for aid in root_artefact_ids}
    frontier = list(root_artefact_ids)
    while frontier:
        children = (
            db.session.query(Artefact.id, Artefact.parent_artefact_id)
            .filter(Artefact.parent_artefact_id.in_(frontier))
            .all()
        )
        if not children:
            break
        next_frontier = []
        for child_id, parent_id in children:
            if child_id in descendant_to_root:
                continue  # defensive: avoid loops
            descendant_to_root[child_id] = descendant_to_root[parent_id]
            next_frontier.append(child_id)
        frontier = next_frontier

    rows = (
        db.session.query(Analysis.artefact_id, Analysis.status, func.count(Analysis.id))
        .filter(Analysis.artefact_id.in_(descendant_to_root.keys()))
        .group_by(Analysis.artefact_id, Analysis.status)
        .all()
    )
    by_root: dict[int, dict[AnalysisStatus, int]] = {}
    for artefact_id, status, count in rows:
        root = descendant_to_root[artefact_id]
        bucket = by_root.setdefault(root, {})
        bucket[status] = bucket.get(status, 0) + count

    priority = (
        AnalysisStatus.RUNNING,
        AnalysisStatus.FAILED,
        AnalysisStatus.PENDING,
        AnalysisStatus.COMPLETED,
    )
    result = {}
    for root_id, counts in by_root.items():
        total = sum(counts.values())
        for status in priority:
            if counts.get(status):
                result[root_id] = (status.value, counts[status], total)
                break
    return result


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


def _render_item_form(form, **kwargs):
    """Render the item create/edit form, injecting the full tag list for autocomplete."""
    kwargs.setdefault('all_tags', Tag.all_for_picker())
    return render_template('items/form.html', form=form, **kwargs)


@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new():
    """Create a new item."""
    form = ItemForm()
    # The creator is always the owner; no owner picker on creation.
    del form['owner_id']

    form.platform_id.choices = indented_taxonomy_choices(Platform, '-- Select Platform --')
    form.category_id.choices = indented_taxonomy_choices(Category, '-- Select Category --')
    form.parent_id.choices = item_parent_choice_list('-- No parent (root item) --', viewer=current_user)

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
        new_parent_id = form.parent_id.data if form.parent_id.data != 0 else None
        if new_parent_id is not None:
            parent = db.session.get(Item, new_parent_id)
            if parent is None or not can_view_item(parent, current_user):
                flash('Parent item not found.', 'danger')
                return _render_item_form(form, title='New Item',
                                       preset_parent=preset_parent, can_set_private=True)
            if parent.private_effective and not can_contribute_to_item(parent, current_user):
                flash('Only the parent owner or an administrator may create child items under that parent.', 'danger')
                return _render_item_form(form, title='New Item',
                                       preset_parent=preset_parent, can_set_private=True)

        item = Item()
        assign_item_fields(
            item,
            name=form.name.data,
            description=form.description.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            category_id=form.category_id.data if form.category_id.data != 0 else None,
            parent_id=new_parent_id,
        )
        assign_item_tags(item, form.tags.data)
        item.owner_id = current_user.id
        item.is_private = form.is_private.data

        db.session.add(item)
        db.session.flush()
        recompute_item_privacy(item)
        db.session.commit()
        get_or_create_slug(item, 'name')

        flash(f'Item "{item.name}" created successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return _render_item_form(form, title='New Item',
                           preset_parent=preset_parent, can_set_private=True)


@blueprint.route('/<string:uuid>')
@public_readable
@require_visible_item()
def view(uuid, item):
    """View an item and its artefacts."""
    if uuid != item.url_id:
        loc = url_for(f'{ROUTENAME}.view', uuid=item.url_id)
        if request.query_string:
            loc += '?' + request.query_string.decode()
        return redirect(loc, 301)

    per_page, page, view_all = resolve_per_page('ARTEFACTS_PER_PAGE', 25)

    artefact_sort = resolve_sort('artefact_sort', _ARTEFACT_SORT_OPTIONS, 'artefacts_sort', 'label_asc')
    artefact_q = request.args.get('artefact_q', '').strip()

    artefact_query = (
        Artefact.query
        .filter_by(item_id=item.id, parent_artefact_id=None)
        .join(Item, Artefact.item_id == Item.id)
        .filter(artefact_visibility_clause(current_user))
        .options(selectinload(Artefact.derived_artefacts))
    )

    if artefact_q:
        artefact_query = artefact_query.filter(Artefact.label.ilike(f'%{artefact_q}%'))

    # Compute letter-to-page mapping for A-Z jump bar (only meaningful for label sorts, not when filtering)
    if artefact_sort in ('label_asc', 'label_desc') and not artefact_q:
        letter_pages, current_letter = compute_letter_pages(
            artefact_query, Artefact.label, per_page, current_page=page,
            descending=(artefact_sort == 'label_desc')
        )
    else:
        letter_pages, current_letter = {}, None

    artefacts_page = artefact_query.order_by(_ARTEFACT_SORT_OPTIONS[artefact_sort]).paginate(page=page, per_page=per_page)

    artefact_analysis_status = _compute_artefact_analysis_status(
        [a.id for a in artefacts_page.items]
    )

    user_can_manage_shares = can_manage_shares(item, current_user)
    shareable_users = []
    shareable_groups = []
    if user_can_manage_shares:
        shareable_users = User.query.filter(User.id != item.owner_id).order_by(User.username).all()
        shareable_groups = Group.query.filter(
            ~func.lower(Group.name).startswith('arcology-')
        ).order_by(Group.name).all()

    # Per-item write gates (mirrors route guards in edit() and delete()).
    # Also require global read_write permission so a read-only admin
    # (is_admin=True, permission=READ_ONLY) doesn't see buttons that 403.
    can_write_globally = current_user.is_authenticated and current_user.has_permission(UserPermission.READ_WRITE)
    user_can_contribute = can_write_globally and (not item.private_effective or can_contribute_to_item(item, current_user))
    user_can_edit = can_write_globally and (not item.private_effective or can_curate_item(item, current_user))
    user_can_delete = can_write_globally and (not item.private_effective or can_change_owner(item, current_user))

    return render_template('items/view.html', item=item, artefacts_page=artefacts_page,
                           letter_pages=letter_pages, current_letter=current_letter,
                           valid_per_page=VALID_PER_PAGE, view_all=view_all,
                           artefact_sort=artefact_sort, artefact_q=artefact_q,
                           artefact_analysis_status=artefact_analysis_status,
                           shares=item.shares,
                           user_can_manage_shares=user_can_manage_shares,
                           shareable_users=shareable_users,
                           shareable_groups=shareable_groups,
                           user_can_contribute=user_can_contribute,
                           user_can_edit=user_can_edit,
                           user_can_delete=user_can_delete,
                           reprioritise_choices=REPRIORITISE_CHOICES)


@blueprint.route('/<string:uuid>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
# View-only share recipients may view private items but must not edit them
# (contribute=True). Curator shares may edit item metadata without changing
# owner/privacy. Public items remain editable by any read_write user.
@require_visible_item(contribute=True)
def edit(uuid, item):
    """Edit an item (including moving it to a different parent)."""
    if request.method == 'GET' and uuid != item.url_id:
        return redirect(url_for(f'{ROUTENAME}.edit', uuid=item.url_id), 301)
    form = ItemForm(obj=item)

    form.platform_id.choices = indented_taxonomy_choices(Platform, '-- Select Platform --')
    form.category_id.choices = indented_taxonomy_choices(Category, '-- Select Category --')
    # Exclude self and descendants from the parent dropdown to prevent cycles
    form.parent_id.choices = item_parent_choice_list('-- No parent (root item) --', exclude_item=item, viewer=current_user)

    can_priv = can_curate_item(item, current_user) or can_manage_privacy(item, current_user)
    can_own = can_change_owner(item, current_user)
    if can_own:
        form.owner_id.choices = [(0, '-- No owner --')] + [
            (u.id, u.username) for u in User.query.order_by(User.username).all()
        ]
    else:
        del form['owner_id']

    if request.method == 'GET':
        form.tags.data = ', '.join([t.name for t in item.tags])
        form.parent_id.data = item.parent_id or 0
        form.is_private.data = item.is_private
        if can_own:
            form.owner_id.data = item.owner_id or 0

    if form.validate_on_submit():
        new_parent_id = form.parent_id.data if form.parent_id.data != 0 else None

        # Reparenting can change an item's effective privacy via inheritance,
        # so a non-owner could otherwise hide a public item by moving it under
        # their own private one.  Restrict parent changes to owner or admin.
        if new_parent_id != item.parent_id and not can_change_owner(item, current_user):
            flash('Only the owner or an administrator may move this item to a different parent.', 'danger')
            return _render_item_form(form, item=item, title='Edit Item',
                                   preset_parent=None, can_set_private=can_priv)

        if new_parent_id is not None:
            new_parent = db.session.get(Item, new_parent_id)
            if new_parent is None or not can_view_item(new_parent, current_user):
                flash('Parent item not found.', 'danger')
                return _render_item_form(form, item=item, title='Edit Item',
                                       preset_parent=None, can_set_private=can_priv)
            if new_parent.private_effective and not can_contribute_to_item(new_parent, current_user):
                flash('Only the parent owner or an administrator may move items under that parent.', 'danger')
                return _render_item_form(form, item=item, title='Edit Item',
                                       preset_parent=None, can_set_private=can_priv)

        # Cycle prevention: ensure the chosen parent is not a descendant of this item
        if new_parent_id is not None:
            if new_parent and item.is_ancestor_of(new_parent):
                flash('Cannot move an item to one of its own descendants.', 'danger')
                return _render_item_form(form, item=item, title='Edit Item',
                                       preset_parent=None, can_set_private=can_priv)

        assign_item_fields(
            item,
            name=form.name.data,
            description=form.description.data,
            platform_id=form.platform_id.data if form.platform_id.data != 0 else None,
            category_id=form.category_id.data if form.category_id.data != 0 else None,
            parent_id=new_parent_id,
        )
        assign_item_tags(item, form.tags.data)

        # Owner reassignment (owner or admin only). Apply before the privacy
        # claim below so an explicit choice wins over auto-claim.
        if can_own:
            item.owner_id = form.owner_id.data or None

        # Privacy toggle (owner, admin, curator share, or claiming an unowned item).
        # Auto-claim: only when transitioning to private via a natural claim
        # (not a curator share — curators manage content, they don't become owners).
        if can_priv:
            if form.is_private.data and can_claim_item(item, current_user):
                item.owner_id = current_user.id
            item.is_private = form.is_private.data

        item.slug = ensure_unique_slug(generate_slug(item.name), Item, existing_id=item.id)
        db.session.flush()
        recompute_item_privacy(item)
        db.session.commit()

        flash(f'Item "{item.name}" updated successfully.', 'success')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    return _render_item_form(form, item=item, title='Edit Item',
                           preset_parent=None, can_set_private=can_priv)


@blueprint.route('/<string:uuid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
@require_visible_item()
def delete(uuid, item):
    """Delete an item and all its descendants (cascade)."""
    # Share recipients may view private items but must not delete them.
    # Public items remain deletable by any read_write user (pre-existing behaviour).
    if item.private_effective and not can_change_owner(item, current_user):
        abort(403)
    name = item.name
    parent = item.parent

    # Flag the whole subtree pending_deletion (it vanishes from every view
    # immediately) and hand the heavy row deletion to the task runner.
    mark_item_pending_deletion(item)
    queue_item_delete(item)
    db.session.commit()

    flash(f'Item "{name}" is being deleted.', 'success')
    # Redirect to parent if we came from within the hierarchy
    if parent:
        return redirect(url_for(f'{ROUTENAME}.view', uuid=parent.url_id))
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<string:uuid>/references/add', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
@require_visible_item(contribute=True)
def add_reference(uuid, item):
    """Add an external reference to an item."""
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
@require_visible_item('item_uuid', contribute=True)
def delete_reference(item_uuid, ref_id, item):
    """Delete an external reference."""
    ref = db.get_or_404(ExternalReference, ref_id)

    if ref.item_id != item.id:
        flash('Invalid reference.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))

    db.session.delete(ref)
    db.session.commit()

    flash('External reference removed.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))


@blueprint.route('/choices', methods=['GET'])
@login_required
def choices_json():
    """Return the full indented item list as JSON for AJAX selectors."""
    choices = indented_item_choices(viewer=current_user)
    return jsonify([{'id': id_, 'name': name} for id_, name in choices])


@blueprint.route('/<string:uuid>/shares/add', methods=['POST'])
@login_required
@require_permission('read_write')
@require_visible_item()
def add_share(uuid, item):
    """Add a user or group share to a private item."""
    if not can_manage_shares(item, current_user):
        abort(403)
    if not item.private_effective:
        flash('Only private items can be shared.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
    share_type = request.form.get('share_type')  # 'user' or 'group'
    permission = request.form.get('permission', 'viewer')
    if permission not in SHARE_PERMISSIONS:
        flash('Invalid share permission.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
    if permission == 'curator' and not can_change_owner(item, current_user):
        flash('Only the item owner or an administrator may grant curator access.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
    if share_type == 'user':
        user_id = request.form.get('user_id', type=int)
        if not user_id:
            flash('Please select a user.', 'error')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        target_user = db.session.get(User, user_id)
        if not target_user:
            flash('User not found.', 'error')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        if user_id == item.owner_id:
            flash('The item owner already has access.', 'warning')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        share = ItemShare(item_id=item.id, user_id=user_id, permission=permission)
        db.session.add(share)
    elif share_type == 'group':
        group_id = request.form.get('group_id', type=int)
        if not group_id:
            flash('Please select a group.', 'error')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        group = db.session.get(Group, group_id)
        if not group:
            flash('Group not found.', 'error')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        if group.name.lower().startswith('arcology-'):
            flash('Groups with the "arcology-" prefix are reserved for internal use and cannot be used for sharing.', 'error')
            return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
        share = ItemShare(item_id=item.id, group_id=group_id, permission=permission)
        db.session.add(share)
    else:
        flash('Invalid share type.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))
    try:
        db.session.commit()
        flash('Share added.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('This user or group already has access.', 'warning')
    return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))


@blueprint.route('/<string:uuid>/shares/<int:share_id>/remove', methods=['POST'])
@login_required
@require_permission('read_write')
@require_visible_item()
def remove_share(uuid, share_id, item):
    """Remove a share from an item."""
    if not can_manage_shares(item, current_user):
        abort(403)
    share = ItemShare.query.filter_by(id=share_id, item_id=item.id).first_or_404()
    db.session.delete(share)
    db.session.commit()
    flash('Share removed.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', uuid=item.url_id))


# vim: ts=4 sw=4 et
