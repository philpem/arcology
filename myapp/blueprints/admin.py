"""
Arcology - Admin Blueprint

User management and system configuration for administrators.
"""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import BooleanField, PasswordField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, EqualTo, Length, Optional
from ..database import (
    ApiKey,
    Artefact,
    Group,
    Item,
    RestrictionType,
    User,
    UserArtefactBypass,
    UserPermission,
    UserRestrictionBypass,
    group_memberships,
)
from ..extensions import db
from ..utils.web_forms import flash_form_errors, redirect_local

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/admin', template_folder='../templates')


def init_app(app):
    """Admin blueprint init (link shown in right-hand navbar for admins)."""
    pass


@blueprint.before_request
@login_required
def _require_admin_for_all():
    """Gate every admin route: unauthenticated → login redirect; non-admin → 403."""
    if not current_user.is_admin:
        abort(403)


def _route_redirect(endpoint: str, **values):
    """Redirect to a local admin endpoint."""
    return redirect_local(ROUTENAME, endpoint, **values)


# =============================================================================
# Forms
# =============================================================================

PERMISSION_CHOICES = [
    ('read_only',  'Read Only — can view but not modify data'),
    ('read_write', 'Read/Write — full item and artefact access'),
    ('staff',      'Staff — read/write plus taxonomy and hash-DB management'),
]


class UserPermissionForm(FlaskForm):
    permission = SelectField('Permission', coerce=str, choices=PERMISSION_CHOICES)


class CreateUserForm(FlaskForm):
    username         = StringField('Username', validators=[DataRequired(), Length(max=50)])
    password         = PasswordField('Password', validators=[
        DataRequired(),
        Length(min=12, message='Password must be at least 12 characters.'),
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(),
        EqualTo('password', message='Passwords must match.'),
    ])
    is_admin         = BooleanField('Administrator')
    permission       = SelectField('Permission', coerce=str, choices=PERMISSION_CHOICES)
    can_use_api      = BooleanField('API Key Access')


class EditUserForm(FlaskForm):
    username         = StringField('Username', validators=[DataRequired(), Length(max=50)])
    new_password     = PasswordField('New Password', validators=[Optional()])
    confirm_password = PasswordField('Confirm New Password', validators=[Optional()])
    is_admin         = BooleanField('Administrator')
    permission       = SelectField('Permission', coerce=str, choices=PERMISSION_CHOICES)
    can_use_api      = BooleanField('API Key Access')


class GroupForm(FlaskForm):
    name        = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
def index():
    users = User.query.order_by(User.username).all()
    user_ids = [u.id for u in users]
    # Count active API keys per user
    key_counts = {
        u.id: ApiKey.query.filter_by(user_id=u.id, is_active=True).count()
        for u in users
    }
    # Count owned items and artefacts per user (for delete modal warnings)
    item_owner_counts = dict(
        db.session.query(Item.owner_id, func.count(Item.id))
        .filter(Item.owner_id.in_(user_ids))
        .group_by(Item.owner_id)
        .all()
    ) if user_ids else {}
    artefact_owner_counts = dict(
        db.session.query(Artefact.owner_id, func.count(Artefact.id))
        .filter(Artefact.owner_id.in_(user_ids))
        .group_by(Artefact.owner_id)
        .all()
    ) if user_ids else {}
    worker_key = current_app.config.get('WORKER_API_KEY', '')
    perm_forms = {u.id: UserPermissionForm(prefix=f'u{u.id}', permission=u.permission.value) for u in users}
    group_count = Group.query.count()
    return render_template('admin/index.html',
        users=users,
        key_counts=key_counts,
        item_owner_counts=item_owner_counts,
        artefact_owner_counts=artefact_owner_counts,
        worker_key=worker_key,
        perm_forms=perm_forms,
        group_count=group_count,
    )


@blueprint.route('/users/create', methods=['GET', 'POST'])
def create_user():
    form = CreateUserForm()
    if form.validate_on_submit():
        # Check username uniqueness
        if User.query.filter_by(username=form.username.data).first():
            flash(f'Username "{form.username.data}" is already taken.', 'error')
            return render_template('admin/create_user.html', form=form, RestrictionType=RestrictionType)
        user = User(
            username=form.username.data,
            is_admin=form.is_admin.data,
            permission=UserPermission(form.permission.data),
            can_use_api=form.can_use_api.data,
        )
        user.setPassword(form.password.data)
        db.session.add(user)
        db.session.flush()

        # Apply restriction bypass permissions
        for rtype_value in request.form.getlist('restriction_bypasses'):
            try:
                rtype = RestrictionType(rtype_value)
                db.session.add(UserRestrictionBypass(user_id=user.id, restriction_type=rtype))
            except (ValueError, KeyError):
                pass

        db.session.commit()
        flash(f'User "{user.username}" created successfully.', 'success')
        return _route_redirect('index')
    return render_template('admin/create_user.html', form=form, RestrictionType=RestrictionType)


def _render_edit_user(form, user, **kwargs):
    """Render the edit_user page, injecting per-artefact bypass list."""
    artefact_bypasses = (
        UserArtefactBypass.query
        .filter_by(user_id=user.id)
        .order_by(UserArtefactBypass.artefact_id, UserArtefactBypass.restriction_type)
        .all()
    )
    kwargs.setdefault('editing_self', False)
    kwargs.setdefault('RestrictionType', RestrictionType)
    return render_template('admin/edit_user.html', form=form, user=user,
                           artefact_bypasses=artefact_bypasses, **kwargs)


@blueprint.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
def edit_user(user_id):
    if user_id == current_user.id:
        flash('You cannot edit your own account. Use your profile page instead.', 'error')
        return _route_redirect('index')
    user = User.query.get_or_404(user_id)

    if request.method == 'GET':
        form = EditUserForm(
            username=user.username,
            is_admin=user.is_admin,
            permission=user.permission.value,
            can_use_api=user.can_use_api,
        )
        return _render_edit_user(form, user)

    form = EditUserForm()
    if form.validate_on_submit():
        # Check username uniqueness (excluding this user)
        existing = User.query.filter_by(username=form.username.data).first()
        if existing and existing.id != user.id:
            flash(f'Username "{form.username.data}" is already taken.', 'error')
            return _render_edit_user(form, user)

        # Validate and apply password change (local accounts only)
        new_pw = form.new_password.data
        if new_pw and user.oidc_managed:
            flash('Password cannot be changed for SSO-managed accounts.', 'error')
            return _render_edit_user(form, user)
        if new_pw:
            if len(new_pw) < 12:
                flash('Password must be at least 12 characters.', 'error')
                return _render_edit_user(form, user)
            if new_pw != form.confirm_password.data:
                flash('Passwords must match.', 'error')
                return _render_edit_user(form, user)
            user.setPassword(new_pw)

        user.username = form.username.data

        # Permissions for SSO-managed accounts are authoritative from the identity
        # provider and will be overwritten on the user's next login anyway.
        # Editing them here would be misleading and is a security risk (an admin
        # could temporarily elevate a user who is currently logged in).
        if not user.oidc_managed:
            user.permission = UserPermission(form.permission.data)
            user.can_use_api = form.can_use_api.data
            user.is_admin = form.is_admin.data

        # Update restriction bypass permissions
        selected_bypasses = set(request.form.getlist('restriction_bypasses'))
        existing_bypasses = {rb.restriction_type.value: rb for rb in user.restriction_bypasses}
        for rtype in RestrictionType:
            if rtype.value in selected_bypasses and rtype.value not in existing_bypasses:
                db.session.add(UserRestrictionBypass(user_id=user.id, restriction_type=rtype))
            elif rtype.value not in selected_bypasses and rtype.value in existing_bypasses:
                db.session.delete(existing_bypasses[rtype.value])

        db.session.commit()
        flash(f'User "{user.username}" updated successfully.', 'success')
        return _route_redirect('index')

    # Form validation failed
    flash_form_errors(form)
    return _render_edit_user(form, user)


@blueprint.route('/users/<int:user_id>/artefact-bypass/<int:bypass_id>/revoke', methods=['POST'])
def revoke_artefact_bypass(user_id, bypass_id):
    bypass = UserArtefactBypass.query.filter_by(id=bypass_id, user_id=user_id).first_or_404()
    db.session.delete(bypass)
    db.session.commit()
    flash('Per-artefact bypass revoked.', 'success')
    return redirect(url_for('.edit_user', user_id=user_id))


@blueprint.route('/users/<int:user_id>/delete', methods=['POST'])
def delete_user(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return _route_redirect('index')
    user = User.query.get_or_404(user_id)
    username = user.username
    item_count = Item.query.filter_by(owner_id=user.id).count()
    artefact_count = Artefact.query.filter_by(owner_id=user.id).count()
    if item_count or artefact_count:
        flash(
            f'Cannot delete "{username}": they own {item_count} item(s) and '
            f'{artefact_count} artefact(s). '
            f'Use the Ownership Reassignment tool below to transfer their work to '
            f'another user, or release it as unowned, then delete the account.',
            'danger',
        )
        return _route_redirect('index')
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{username}" deleted.', 'success')
    return _route_redirect('index')


@blueprint.route('/users/<int:user_id>/set-permission', methods=['POST'])
def set_permission(user_id):
    user = User.query.get_or_404(user_id)
    if user.oidc_managed:
        flash(f'Permissions for "{user.username}" are managed by SSO and cannot be changed here.', 'error')
        return _route_redirect('index')
    form = UserPermissionForm(prefix=f'u{user_id}')
    if form.validate_on_submit():
        try:
            user.permission = UserPermission(form.permission.data)
            db.session.commit()
            flash(f'Permission for "{user.username}" updated to {user.permission.value}.', 'success')
        except ValueError:
            flash('Invalid permission level.', 'error')
    return _route_redirect('index')


@blueprint.route('/users/<int:user_id>/toggle-api', methods=['POST'])
def toggle_api(user_id):
    user = User.query.get_or_404(user_id)
    if user.oidc_managed:
        flash(f'API access for "{user.username}" is managed by SSO and cannot be changed here.', 'error')
        return _route_redirect('index')
    user.can_use_api = not user.can_use_api
    db.session.commit()
    state = 'enabled' if user.can_use_api else 'disabled'
    flash(f'API key access {state} for "{user.username}".', 'success')
    return _route_redirect('index')


# =============================================================================
# Group management routes
# =============================================================================

@blueprint.route('/groups')
def groups():
    """List all groups with member counts."""
    groups_list = (
        db.session.query(Group, func.count(group_memberships.c.user_id).label('member_count'))
        .outerjoin(group_memberships, Group.id == group_memberships.c.group_id)
        .group_by(Group.id)
        .order_by(Group.name)
        .all()
    )
    all_users = User.query.order_by(User.username).all()
    return render_template('admin/groups.html', groups=groups_list, all_users=all_users)


@blueprint.route('/groups/create', methods=['GET', 'POST'])
def create_group():
    """Create a new group."""
    form = GroupForm()
    if form.validate_on_submit():
        name = form.name.data.strip().lower()
        if name.startswith('arcology-'):
            flash('Group names starting with "arcology-" are reserved for internal use.', 'error')
            return render_template('admin/group_form.html', form=form, title='Create Group')
        if Group.query.filter_by(name=name).first():
            flash(f'A group named "{name}" already exists.', 'error')
            return render_template('admin/group_form.html', form=form, title='Create Group')
        group = Group(name=name, description=form.description.data or None, source='local')
        db.session.add(group)
        db.session.commit()
        flash(f'Group "{group.name}" created.', 'success')
        return _route_redirect('groups')
    return render_template('admin/group_form.html', form=form, title='Create Group')


@blueprint.route('/groups/<int:group_id>/edit', methods=['GET', 'POST'])
def edit_group(group_id):
    """Edit a group's name and description."""
    group = Group.query.get_or_404(group_id)
    if request.method == 'GET' and group.source == 'oidc':
        flash('This group is managed by the identity provider — its name cannot be changed here.', 'warning')
    form = GroupForm(obj=group)
    if form.validate_on_submit():
        name = form.name.data.strip().lower()
        if group.source == 'oidc' and name != group.name:
            flash('Group names managed by the identity provider cannot be changed here.', 'warning')
            return render_template('admin/group_form.html', form=form, title='Edit Group', group=group)
        if name.startswith('arcology-'):
            flash('Group names starting with "arcology-" are reserved for internal use.', 'error')
            return render_template('admin/group_form.html', form=form, title='Edit Group', group=group)
        existing = Group.query.filter_by(name=name).first()
        if existing and existing.id != group.id:
            flash(f'A group named "{name}" already exists.', 'error')
            return render_template('admin/group_form.html', form=form, title='Edit Group', group=group)
        if group.source != 'oidc':
            group.name = name
        group.description = form.description.data or None
        db.session.commit()
        flash(f'Group "{group.name}" updated.', 'success')
        return _route_redirect('groups')
    return render_template('admin/group_form.html', form=form, title='Edit Group', group=group)


@blueprint.route('/groups/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    """Delete a group."""
    group = Group.query.get_or_404(group_id)
    name = group.name
    db.session.delete(group)
    db.session.commit()
    flash(f'Group "{name}" deleted.', 'success')
    return _route_redirect('groups')


@blueprint.route('/groups/<int:group_id>/members/add', methods=['POST'])
def add_group_member(group_id):
    """Add a user to a group."""
    group = Group.query.get_or_404(group_id)
    if group.source == 'oidc':
        flash(f'Group "{group.name}" is managed by the identity provider. Membership is synced automatically on login.', 'error')
        return _route_redirect('groups')
    user_id = request.form.get('user_id', type=int)
    if not user_id:
        flash('Please select a user.', 'error')
        return _route_redirect('groups')
    user = User.query.get_or_404(user_id)
    if user not in group.members:
        group.members.append(user)
        db.session.commit()
        flash(f'Added "{user.username}" to group "{group.name}".', 'success')
    else:
        flash(f'"{user.username}" is already a member of "{group.name}".', 'warning')
    return _route_redirect('groups')


@blueprint.route('/groups/<int:group_id>/members/<int:user_id>/remove', methods=['POST'])
def remove_group_member(group_id, user_id):
    """Remove a user from a group."""
    group = Group.query.get_or_404(group_id)
    if group.source == 'oidc':
        flash(f'Group "{group.name}" is managed by the identity provider. Membership is synced automatically on login.', 'error')
        return _route_redirect('groups')
    user = User.query.get_or_404(user_id)
    if user in group.members:
        group.members.remove(user)
        db.session.commit()
        flash(f'Removed "{user.username}" from group "{group.name}".', 'success')
    else:
        flash(f'"{user.username}" is not a member of "{group.name}".', 'warning')
    return _route_redirect('groups')


# =============================================================================
# Ownership reassignment
# =============================================================================

class ReassignOwnershipForm(FlaskForm):
    from_user_id = SelectField('From user', coerce=int)
    to_user_id   = SelectField('To user', coerce=int)


@blueprint.route('/reassign-ownership', methods=['GET', 'POST'])
def reassign_ownership():
    """Bulk-transfer all items and artefacts from one user to another."""
    users = User.query.order_by(User.username).all()
    user_choices = [(0, '— Unowned —')] + [(u.id, u.username) for u in users]

    form = ReassignOwnershipForm()
    form.from_user_id.choices = user_choices
    form.to_user_id.choices   = user_choices

    # Pre-select the source user when linked from the delete-user warning.
    if request.method == 'GET':
        from_arg = request.args.get('from_user', type=int)
        if from_arg is not None and any(choice_id == from_arg for choice_id, _ in user_choices):
            form.from_user_id.data = from_arg

    preview = None

    if form.validate_on_submit():
        from_id = form.from_user_id.data  # 0 means unowned (owner_id IS NULL)
        to_id   = form.to_user_id.data    # 0 means unowned

        if from_id == to_id:
            flash('Source and destination must be different.', 'error')
            return render_template('admin/reassign_ownership.html', form=form, preview=None)

        from_user = db.session.get(User, from_id) if from_id else None
        to_user   = db.session.get(User, to_id)   if to_id   else None

        if from_id and not from_user:
            flash('Source user not found.', 'error')
            return render_template('admin/reassign_ownership.html', form=form, preview=None)
        if to_id and not to_user:
            flash('Destination user not found.', 'error')
            return render_template('admin/reassign_ownership.html', form=form, preview=None)

        item_q     = Item.query.filter(Item.owner_id == from_user.id if from_user else Item.owner_id.is_(None))
        artefact_q = Artefact.query.filter(Artefact.owner_id == from_user.id if from_user else Artefact.owner_id.is_(None))
        item_count     = item_q.count()
        artefact_count = artefact_q.count()

        if request.form.get('confirmed') == '1':
            new_owner_id = to_user.id if to_user else None
            item_q.update({'owner_id': new_owner_id})
            artefact_q.update({'owner_id': new_owner_id})
            db.session.commit()
            from_label = from_user.username if from_user else 'unowned'
            to_label   = to_user.username   if to_user   else 'unowned'
            flash(
                f'Reassigned {item_count} item(s) and {artefact_count} artefact(s) '
                f'from {from_label} to {to_label}.',
                'success',
            )
            return _route_redirect('index')

        preview = {
            'from_user':      from_user,
            'to_user':        to_user,
            'from_id':        from_id,
            'to_id':          to_id,
            'item_count':     item_count,
            'artefact_count': artefact_count,
        }

    return render_template('admin/reassign_ownership.html', form=form, preview=preview)

# vim: ts=4 sw=4 et
