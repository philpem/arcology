"""
Arcology - Authentication Blueprint

Local-login / logout routes plus Flask-Login session management.
Separate from app.py so the factory stays slim.
"""

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired
from ..database import User
from ..extensions import login_manager
from ..utils.config import bool_config
from ..utils.safe_redirect import safe_redirect_path

ROUTENAME = 'auth'

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='')


def init_app(app):
    """Register Flask-Login user_loader and update login_view endpoint."""
    login_manager.login_view = 'auth.login'

    @login_manager.user_loader
    def load_user(userid):
        userrec = None
        try:
            userrec = User.query.filter(User.id == int(userid)).one()
        except MultipleResultsFound:
            app.logger.error(
                "USER LOGIN FAILURE: User id '%s' has a doppelganger (duplicate id found)", userid)
        except NoResultFound:
            pass
        return userrec


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


@blueprint.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()

    # Enforce LOCAL_LOGIN_ENABLED server-side (the template merely hides the form).
    local_login_on = bool_config('LOCAL_LOGIN_ENABLED', default=True)
    if not local_login_on and form.is_submitted():
        abort(403)

    # Auto-redirect to SSO on GET when local login is disabled, OIDC is
    # enabled, OIDC_AUTO_REDIRECT is on, and no flash messages are pending
    # (flash messages must be shown before bouncing the user away).
    if request.method == 'GET' and not local_login_on:
        oidc_on = bool_config('OIDC_ENABLED')
        auto_redir = bool_config('OIDC_AUTO_REDIRECT', default=True)
        if oidc_on and auto_redir and not session.get('_flashes'):
            next_url = request.args.get('next', '')
            return redirect(url_for('myapp_blueprints_oidc_auth.sso_login', next=next_url))

    if form.validate_on_submit():
        userrec = None
        try:
            userrec = User.query.filter(User.username == form.username.data).one()
        except MultipleResultsFound:
            current_app.logger.error(
                "USER LOGIN FAILURE: User '%s' has a doppelganger (duplicate username found)",
                form.username.data)
        except NoResultFound:
            pass

        if userrec is not None:
            if userrec.checkPassword(form.password.data):
                login_user(userrec)
                # Redirect to the page the user was trying to reach, or the dashboard.
                # SECURITY: confine next= to a same-origin relative path.  A naive
                # startswith('/') check is bypassable via browser normalisation
                # (e.g. /\evil.com -> //evil.com, /%09/evil.com -> //evil.com);
                # is_safe_redirect_path handles control chars and backslashes.
                next_url = safe_redirect_path(
                    request.args.get("next"),
                    url_for("myapp_blueprints_dashboard.index"),
                )
                return redirect(next_url)

    if request.method == 'POST':
        flash("Error logging in - please check your username and password and ensure that CAPS LOCK is turned off.", "error")
    return render_template("login.html", form=form)


@blueprint.route("/logout")
@login_required
def logout():
    # Delegate to the SSO logout route when single-logout is configured,
    # so the provider session is also terminated.
    if bool_config('OIDC_SINGLE_LOGOUT') and session.get('oidc_end_session_endpoint'):
        return redirect(url_for('myapp_blueprints_oidc_auth.sso_logout'))
    logout_user()
    flash("You have now been logged out.", "info")
    return redirect(url_for("auth.login"))

# vim: ts=4 sw=4 et
