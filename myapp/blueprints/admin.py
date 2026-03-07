"""
Arcology - Admin Blueprint

User management and system configuration for administrators.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField, StringField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Length, EqualTo, Optional

from ..extensions import db
from ..database import User, ApiKey, UserPermission

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/admin', template_folder='../templates')


def init_app(app):
	"""Register admin menu item."""
	app.add_menu_item("Admin", f"{ROUTENAME}.index", 1000)


def _require_admin():
	"""Abort with 403 if the current user is not an admin."""
	if not current_user.is_authenticated or not current_user.is_admin:
		abort(403)


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
			return render_template('admin/create_user.html', form=form)
		user = User(
			username=form.username.data,
			is_admin=form.is_admin.data,
			permission=UserPermission(form.permission.data),
			can_use_api=form.can_use_api.data,
		)
		user.setPassword(form.password.data)
		db.session.add(user)
		db.session.commit()
		flash(f'User "{user.username}" created successfully.', 'success')
		return redirect(url_for(f'{ROUTENAME}.index'))
	return render_template('admin/create_user.html', form=form)


@blueprint.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
	_require_admin()
	user = User.query.get_or_404(user_id)
	editing_self = (user.id == current_user.id)

	if request.method == 'GET':
		form = EditUserForm(
			username=user.username,
			is_admin=user.is_admin,
			permission=user.permission.value,
			can_use_api=user.can_use_api,
		)
		return render_template('admin/edit_user.html', form=form, user=user, editing_self=editing_self)

	form = EditUserForm()
	if form.validate_on_submit():
		# Check username uniqueness (excluding this user)
		existing = User.query.filter_by(username=form.username.data).first()
		if existing and existing.id != user.id:
			flash(f'Username "{form.username.data}" is already taken.', 'error')
			return render_template('admin/edit_user.html', form=form, user=user, editing_self=editing_self)

		# Validate password if provided
		new_pw = form.new_password.data
		if new_pw:
			if len(new_pw) < 12:
				flash('Password must be at least 12 characters.', 'error')
				return render_template('admin/edit_user.html', form=form, user=user, editing_self=editing_self)
			if new_pw != form.confirm_password.data:
				flash('Passwords must match.', 'error')
				return render_template('admin/edit_user.html', form=form, user=user, editing_self=editing_self)
			user.setPassword(new_pw)

		user.username = form.username.data
		user.permission = UserPermission(form.permission.data)
		user.can_use_api = form.can_use_api.data

		# Prevent admins from removing their own admin status
		if not editing_self:
			user.is_admin = form.is_admin.data

		db.session.commit()
		flash(f'User "{user.username}" updated successfully.', 'success')
		return redirect(url_for(f'{ROUTENAME}.index'))

	# Form validation failed
	for field, errors in form.errors.items():
		for error in errors:
			flash(error, 'error')
	return render_template('admin/edit_user.html', form=form, user=user, editing_self=editing_self)


@blueprint.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
	_require_admin()
	if user_id == current_user.id:
		flash('You cannot delete your own account.', 'error')
		return redirect(url_for(f'{ROUTENAME}.index'))
	user = User.query.get_or_404(user_id)
	username = user.username
	db.session.delete(user)
	db.session.commit()
	flash(f'User "{username}" deleted.', 'success')
	return redirect(url_for(f'{ROUTENAME}.index'))


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
	return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/users/<int:user_id>/toggle-api', methods=['POST'])
@login_required
def toggle_api(user_id):
	_require_admin()
	user = User.query.get_or_404(user_id)
	user.can_use_api = not user.can_use_api
	db.session.commit()
	state = 'enabled' if user.can_use_api else 'disabled'
	flash(f'API key access {state} for "{user.username}".', 'success')
	return redirect(url_for(f'{ROUTENAME}.index'))

# vim: ts=4 sw=4 noet
