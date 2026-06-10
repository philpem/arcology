"""
Arcology - Authentication Blueprint

Local-login / logout routes plus Flask-Login session management.
Separate from app.py so the factory stays slim.
"""

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired

ROUTENAME = 'auth'

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='')


def init_app(app):
    """Register Flask-Login user_loader and update login_view endpoint."""
    from ..database import User
    from ..extensions import login_manager

    login_manager.login_view = 'auth.login'

    @login_manager.user_loader
    def load_user(userid):
        userrec = None
        try:
            userrec = User.query.filter(User.id == int(userid)).one()
        except MultipleResultsFound:
            app.logger.error(
                "USER LOGIN FAILURE: User '%s' has a doppelganger (duplicate username found)")
        except NoResultFound:
            pass
        return userrec


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


@blueprint.route("/login", methods=["GET", "POST"])
def login():
    from ..database import User

    form = LoginForm()

    # Enforce LOCAL_LOGIN_ENABLED server-side (the template merely hides the form).
    from flask import current_app
    local_login_on = current_app.config.get('LOCAL_LOGIN_ENABLED', True)
    if isinstance(local_login_on, str):
        local_login_on = local_login_on.lower() in ('1', 'true', 'yes')
    if not local_login_on and form.is_submitted():
        abort(403)

    # Auto-redirect to SSO on GET when local login is disabled, OIDC is
    # enabled, OIDC_AUTO_REDIRECT is on, and no flash messages are pending
    # (flash messages must be shown before bouncing the user away).
    if request.method == 'GET' and not local_login_on:
        oidc_on = current_app.config.get('OIDC_ENABLED', False)
        if isinstance(oidc_on, str):
            oidc_on = oidc_on.lower() in ('1', 'true', 'yes')
        auto_redir = current_app.config.get('OIDC_AUTO_REDIRECT', True)
        if isinstance(auto_redir, str):
            auto_redir = auto_redir.lower() in ('1', 'true', 'yes')
        if oidc_on and auto_redir and not session.get('_flashes'):
            next_url = request.args.get('next', '')
            return redirect(url_for('myapp_blueprints_oidc_auth.sso_login', next=next_url))

    if form.validate_on_submit():
        userrec = None
        try:
            userrec = User.query.filter(User.username == form.username.data).one()
        except MultipleResultsFound:
            from flask import current_app as _app
            _app.logger.error(
                "USER LOGIN FAILURE: User '%s' has a doppelganger (duplicate username found)")
        except NoResultFound:
            pass

        if userrec is not None:
            if userrec.checkPassword(form.password.data):
                login_user(userrec)
                # Redirect to the page the user was trying to reach, or the dashboard.
                # SECURITY: reject any next= URL without an absolute same-origin path.
                # urlparse alone does not catch browser-normalised open-redirects like
                # /\evil.com, so require the URL to start with a single '/'.
                next_url = request.args.get("next")
                if next_url and (not next_url.startswith('/') or next_url.startswith('//')):
                    next_url = None
                return redirect(next_url or url_for("myapp_blueprints_dashboard.index"))

    if request.method == 'POST':
        flash("Error logging in - please check your username and password and ensure that CAPS LOCK is turned off.", "error")
    return render_template("login.html", form=form)


@blueprint.route("/logout")
@login_required
def logout():
    from flask import current_app
    # Delegate to the SSO logout route when single-logout is configured,
    # so the provider session is also terminated.
    oidc_single_logout = current_app.config.get('OIDC_SINGLE_LOGOUT', False)
    if isinstance(oidc_single_logout, str):
        oidc_single_logout = oidc_single_logout.lower() in ('1', 'true', 'yes')
    if oidc_single_logout and session.get('oidc_end_session_endpoint'):
        return redirect(url_for('myapp_blueprints_oidc_auth.sso_logout'))
    logout_user()
    flash("You have now been logged out.", "info")
    return redirect(url_for("auth.login"))

# vim: ts=4 sw=4 et
