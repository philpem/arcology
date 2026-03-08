"""
Arcology - Profile Blueprint

User profile management: change password and manage API application keys.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, SubmitField
from wtforms.validators import DataRequired, Length, EqualTo, ValidationError

from ..extensions import db
from ..database import ApiKey, ApiKeyPermission, UserPermission

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/profile', template_folder='../templates')


def init_app(app):
    """Register profile menu item."""
    app.add_menu_item("Profile", f"{ROUTENAME}.index", 900)


# =============================================================================
# Forms
# =============================================================================

class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password     = PasswordField('New Password',     validators=[
        DataRequired(),
        Length(min=12, message='Password must be at least 12 characters.'),
    ])
    confirm_password = PasswordField('Confirm New Password', validators=[
        DataRequired(),
        EqualTo('new_password', message='Passwords must match.'),
    ])


class CreateKeyForm(FlaskForm):
    name       = StringField('Key Name', validators=[DataRequired(), Length(max=100)])
    permission = SelectField('Permission', coerce=str)


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    pwd_form = ChangePasswordForm()
    key_form = None

    if current_user.can_use_api:
        key_form = CreateKeyForm()
        key_form.permission.choices = _permission_choices()

    keys = (
        ApiKey.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(ApiKey.created_at.desc())
        .all()
    ) if current_user.can_use_api else []

    return render_template('profile/index.html',
        pwd_form=pwd_form,
        key_form=key_form,
        keys=keys,
    )


@blueprint.route('/change-password', methods=['POST'])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.checkPassword(form.current_password.data):
            flash('Current password is incorrect.', 'error')
            return redirect(url_for(f'{ROUTENAME}.index'))
        current_user.setPassword(form.new_password.data)
        db.session.commit()
        flash('Password changed successfully.', 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{error}', 'error')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/keys/create', methods=['POST'])
@login_required
def create_key():
    if not current_user.can_use_api:
        abort(403)

    form = CreateKeyForm()
    form.permission.choices = _permission_choices()

    if form.validate_on_submit():
        try:
            permission = ApiKeyPermission(form.permission.data)
        except ValueError:
            flash('Invalid permission level.', 'error')
            return redirect(url_for(f'{ROUTENAME}.index'))

        # Ensure the requested permission doesn't exceed the user's own level
        if not _key_permission_allowed(permission):
            flash('Permission level exceeds your account access.', 'error')
            return redirect(url_for(f'{ROUTENAME}.index'))

        key, raw_key = ApiKey.create(
            user_id=current_user.id,
            name=form.name.data,
            permission=permission,
        )
        db.session.add(key)
        db.session.commit()

        # Store the raw key in the session for one-time display, then redirect
        session['new_api_key'] = raw_key
        return redirect(url_for(f'{ROUTENAME}.key_created'))

    for field, errors in form.errors.items():
        for error in errors:
            flash(f'{error}', 'error')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/keys/created')
@login_required
def key_created():
    raw_key = session.pop('new_api_key', None)
    if not raw_key:
        abort(404)
    return render_template('profile/key_created.html', raw_key=raw_key)


@blueprint.route('/keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def revoke_key(key_id):
    key = ApiKey.query.get_or_404(key_id)
    if key.user_id != current_user.id:
        abort(403)
    key.is_active = False
    db.session.commit()
    flash(f'Key "{key.name}" revoked.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


# =============================================================================
# Helpers
# =============================================================================

def _permission_choices() -> list[tuple[str, str]]:
    """
    Return SelectField choices for API key permission, limited to what
    the current user's own permission level allows.
    """
    all_choices = [
        (ApiKeyPermission.READ_ONLY.value,   'Read Only — GET requests only'),
        (ApiKeyPermission.READ_UPLOAD.value, 'Read + Upload — create items & upload artefacts'),
        (ApiKeyPermission.READ_WRITE.value,  'Full Read/Write — complete access'),
    ]
    if current_user.permission == UserPermission.READ_ONLY:
        # read-only users can only create read-only keys
        return all_choices[:1]
    # read-write users can create keys at any level
    return all_choices

def _key_permission_allowed(permission: ApiKeyPermission) -> bool:
    """Return True if the current user is allowed to create a key at this level."""
    if current_user.permission == UserPermission.READ_ONLY:
        return permission == ApiKeyPermission.READ_ONLY
    return True  # READ_WRITE users can create any level

# vim: ts=4 sw=4 et
