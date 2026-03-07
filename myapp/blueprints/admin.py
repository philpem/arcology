"""
Arcology - Admin Blueprint

User management and system configuration for administrators.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField

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

class UserPermissionForm(FlaskForm):
	permission = SelectField('Permission', coerce=str, choices=[
		('read_only',  'Read Only — can view but not modify data'),
		('read_write', 'Full Read/Write — complete access'),
	])


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
