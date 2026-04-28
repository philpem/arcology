"""
Arcology - Admin Blueprint

User management and system configuration for administrators.
"""

from flask import Blueprint, abort, current_app, flash, render_template, request
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, EqualTo, Length, Optional

from ..database import ApiKey, RestrictionType, User, UserPermission, UserRestrictionBypass
from ..extensions import db
from ..utils.web_forms import flash_form_errors, redirect_local

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/admin', template_folder='../templates')


def init_app(app):
    """Admin blueprint init (link shown in right-hand navbar for admins)."""
    pass


def _require_admin():
    """Abort with 403 if the current user is not an admin."""
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def _route_redirect(endpoint: str, **values):
    """Redirect to a local admin endpoint."""
    return redirect_local(ROUTENAME, endpoint, **values)


# =============================================================================
# Forms
# =============================================================================

PERMISSION_CHOICES = [
    ('read_only',  'Read Only — can view but not modify data'),
    ('read_write', 'Full Read/Write — complete access'),
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


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    _require_admin()
    users = User.query.order_by(User.username).all()
    # Count active API keys per user
    key_counts = {
        u.id: ApiKey.query.filter_by(user_id=u.id, is_active=True).count()
        for u in users
    }
    worker_key = current_app.config.get('WORKER_API_KEY', '')
    perm_forms = {u.id: UserPermissionForm(prefix=f'u{u.id}', permission=u.permission.value) for u in users}
    return render_template('admin/index.html',
        users=users,
        key_counts=key_counts,
        worker_key=worker_key,
        perm_forms=perm_forms,
    )


@blueprint.route('/users/create', methods=['GET', 'POST'])
@login_required
def create_user():
    _require_admin()
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


@blueprint.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    _require_admin()
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
        return render_template('admin/edit_user.html', form=form, user=user,
                               editing_self=False, RestrictionType=RestrictionType)

    form = EditUserForm()
    if form.validate_on_submit():
        # Check username uniqueness (excluding this user)
        existing = User.query.filter_by(username=form.username.data).first()
        if existing and existing.id != user.id:
            flash(f'Username "{form.username.data}" is already taken.', 'error')
            return render_template('admin/edit_user.html', form=form, user=user, editing_self=False, RestrictionType=RestrictionType)

        # Validate password if provided
        new_pw = form.new_password.data
        if new_pw:
            if len(new_pw) < 12:
                flash('Password must be at least 12 characters.', 'error')
                return render_template('admin/edit_user.html', form=form, user=user, editing_self=False, RestrictionType=RestrictionType)
            if new_pw != form.confirm_password.data:
                flash('Passwords must match.', 'error')
                return render_template('admin/edit_user.html', form=form, user=user, editing_self=False, RestrictionType=RestrictionType)
            user.setPassword(new_pw)

        user.username = form.username.data
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
    return render_template('admin/edit_user.html', form=form, user=user, editing_self=False, RestrictionType=RestrictionType)


@blueprint.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    _require_admin()
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return _route_redirect('index')
    user = User.query.get_or_404(user_id)
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{username}" deleted.', 'success')
    return _route_redirect('index')


@blueprint.route('/users/<int:user_id>/set-permission', methods=['POST'])
@login_required
def set_permission(user_id):
    _require_admin()
    user = User.query.get_or_404(user_id)
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
@login_required
def toggle_api(user_id):
    _require_admin()
    user = User.query.get_or_404(user_id)
    user.can_use_api = not user.can_use_api
    db.session.commit()
    state = 'enabled' if user.can_use_api else 'disabled'
    flash(f'API key access {state} for "{user.username}".', 'success')
    return _route_redirect('index')

# vim: ts=4 sw=4 et
