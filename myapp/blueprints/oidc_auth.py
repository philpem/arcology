"""
Arcology - OIDC SSO Authentication Blueprint

Handles OAuth 2.0 / OpenID Connect login via an external identity provider
(Keycloak, Okta, Azure AD, etc.).  Activated by setting OIDC_ENABLED = True
in myapp.cfg or via the OIDC_ENABLED environment variable.

Routes:
  GET /auth/sso/login     — redirect to the identity provider
  GET /auth/sso/callback  — handle the authorisation code exchange
  GET /auth/sso/logout    — local logout (+ provider single-logout if configured)
"""

from urllib.parse import quote as urlquote
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from ..database import User, UserPermission
from ..extensions import db

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/auth',
                      template_folder='../templates')

_oauth = OAuth()


# =============================================================================
# Blueprint initialisation (called by _register_blueprints in app.py)
# =============================================================================

def init_app(app):
    """Register the Authlib OAuth client and inject SSO context into templates."""
    _oauth.init_app(app)

    @app.context_processor
    def _inject_oidc():
        return dict(
            oidc_enabled=_bool_cfg(app, 'OIDC_ENABLED'),
            oidc_provider_name=app.config.get('OIDC_PROVIDER_NAME', 'SSO'),
            local_login_enabled=_bool_cfg(app, 'LOCAL_LOGIN_ENABLED', default=True),
        )

    if not _bool_cfg(app, 'OIDC_ENABLED'):
        return

    discovery_url = app.config.get('OIDC_DISCOVERY_URL', '')
    if not discovery_url:
        app.logger.warning(
            'OIDC_ENABLED is True but OIDC_DISCOVERY_URL is not configured'
        )
        return

    _oauth.register(
        name='oidc',
        client_id=app.config.get('OIDC_CLIENT_ID', ''),
        client_secret=app.config.get('OIDC_CLIENT_SECRET', ''),
        server_metadata_url=discovery_url,
        client_kwargs={
            'scope': app.config.get('OIDC_SCOPES', 'openid profile email'),
            'code_challenge_method': 'S256',
        },
    )


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/sso/login')
def sso_login():
    if not _bool_cfg(current_app, 'OIDC_ENABLED'):
        abort(404)
    next_url = request.args.get('next', '')
    if next_url and next_url.startswith('/') and not next_url.startswith('//'):
        session['oidc_next'] = next_url
    redirect_uri = url_for('.sso_callback', _external=True)
    try:
        return _oauth.oidc.authorize_redirect(redirect_uri)
    except Exception as exc:
        current_app.logger.warning('OIDC provider unreachable during login: %s', exc)
        flash('SSO provider is unavailable. Please try again or contact an administrator.', 'error')
        return redirect(url_for('login'))


@blueprint.route('/sso/callback')
def sso_callback():
    if not _bool_cfg(current_app, 'OIDC_ENABLED'):
        abort(404)

    try:
        token = _oauth.oidc.authorize_access_token()
    except Exception as exc:
        current_app.logger.warning('OIDC callback error: %s', exc)
        flash('SSO login failed. Please try again or contact an administrator.', 'error')
        return redirect(url_for('login'))

    userinfo = token.get('userinfo') or {}
    if not userinfo:
        try:
            userinfo = _oauth.oidc.userinfo(token=token)
        except Exception as exc:
            current_app.logger.warning('OIDC userinfo fetch failed: %s', exc)
            flash('SSO login failed: could not retrieve user information.', 'error')
            return redirect(url_for('login'))

    user, error_msg = _get_or_create_user(userinfo)
    if user is None:
        flash(error_msg or 'Your account is not authorised to access this application.',
              'error')
        return _redirect_clearing_provider_session(token)

    role_matched = _sync_permissions(user, userinfo)
    if not role_matched and _bool_cfg(current_app, 'OIDC_REQUIRE_ROLE'):
        db.session.rollback()
        flash('Your account is not authorised to access this application. '
              'Please contact your system administrator.',
              'error')
        return _redirect_clearing_provider_session(token)

    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent first-login race: another request inserted the same oidc_sub
        # or username between our lookup and commit. Retry the lookup.
        db.session.rollback()
        try:
            user = User.query.filter_by(oidc_sub=userinfo.get('sub')).one()
        except Exception:
            current_app.logger.warning('OIDC concurrent login race could not be resolved')
            flash('SSO login failed. Please try again.', 'error')
            return redirect(url_for('login'))

    login_user(user)
    session.permanent = True

    # Cache provider end-session endpoint and ID token only when single logout is
    # enabled — the ID token can be sizeable and is not needed otherwise.
    if _bool_cfg(current_app, 'OIDC_SINGLE_LOGOUT'):
        try:
            metadata = _oauth.oidc.load_server_metadata()
            end_session = metadata.get('end_session_endpoint')
            if end_session:
                session['oidc_end_session_endpoint'] = end_session
            id_token = token.get('id_token')
            if id_token:
                session['oidc_id_token'] = id_token
        except Exception:
            pass

    next_url = session.pop('oidc_next', None)
    return redirect(next_url or url_for('myapp_blueprints_dashboard.index'))


@blueprint.route('/sso/logout')
@login_required
def sso_logout():
    """Log out of Arcology, and optionally terminate the provider session too."""
    end_session_endpoint = session.get('oidc_end_session_endpoint')
    id_token = session.get('oidc_id_token')

    logout_user()
    flash('You have been logged out.', 'info')

    if _bool_cfg(current_app, 'OIDC_SINGLE_LOGOUT') and end_session_endpoint:
        post_logout_uri = url_for('login', _external=True)
        sep = '&' if '?' in end_session_endpoint else '?'
        target = (
            f'{end_session_endpoint}{sep}'
            f'post_logout_redirect_uri={urlquote(post_logout_uri, safe="")}'
        )
        if id_token:
            target += f'&id_token_hint={urlquote(id_token, safe="")}'
        return redirect(target)

    return redirect(url_for('login'))


# =============================================================================
# Internal helpers
# =============================================================================

def _bool_cfg(app_or_current_app, key: str, default: bool = False) -> bool:
    """Read a config key that may be a Python bool or an env-var string."""
    val = app_or_current_app.config.get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).lower() in ('1', 'true', 'yes')


def _get_or_create_user(userinfo: dict) -> tuple['User | None', 'str | None']:
    """Resolve a User record from OIDC userinfo, creating one if needed.

    Returns (user, None) on success, (None, error_message) on failure.

    Resolution order:
    1. Look up by oidc_sub (fast path for returning SSO users).
    2. Look up by the claim configured in OIDC_MATCH_CLAIM (default:
       preferred_username → username) and link the account.
    3. Auto-provision a new local account from the token claims.
    """
    sub = userinfo.get('sub', '')
    if not sub:
        return None, 'SSO token is missing the required subject (sub) claim.'

    # 1. Already linked
    try:
        user = User.query.filter_by(oidc_sub=sub).one()
        return user, None
    except NoResultFound:
        pass
    except MultipleResultsFound:
        current_app.logger.error('Multiple users share oidc_sub=%r — data integrity issue', sub)
        return None, 'Account configuration error. Contact an administrator.'

    # 2. Link an existing local account
    match_claim = current_app.config.get('OIDC_MATCH_CLAIM', 'preferred_username')
    claim_value = userinfo.get(match_claim, '')

    if claim_value and match_claim != 'sub':
        # Refuse to link by email if the provider has not verified it — an unverified
        # email claim could be attacker-controlled, enabling account takeover.
        if match_claim == 'email' and not userinfo.get('email_verified'):
            current_app.logger.warning(
                'OIDC account linking skipped: email claim is unverified for sub=%r', sub
            )
        else:
            user = _find_by_match_claim(match_claim, claim_value)
            if user is not None:
                user.oidc_sub = sub
                user.oidc_managed = True
                # Disable local-password login for the now-SSO-managed account so
                # that revoking the user in the IdP fully removes their access.
                user.password_hash = '!'
                if not user.email and userinfo.get('email'):
                    user.email = userinfo['email']
                current_app.logger.info(
                    'Linked existing user %r to OIDC sub=%r via claim %r=%r',
                    user.username, sub, match_claim, claim_value,
                )
                return user, None

    # 3. Auto-provision
    username = (userinfo.get('preferred_username') or sub)[:50]
    if User.query.filter_by(username=username).first():
        return None, (
            f'Cannot create SSO account: username “{username}” is already '
            'taken by a local account. Ask an administrator to link or rename it.'
        )

    user = User(
        username=username,
        email=userinfo.get('email') or None,
        oidc_sub=sub,
        oidc_managed=True,
        # Sentinel value: bcrypt.checkpw raises ValueError, caught by checkPassword
        password_hash='!',
        is_admin=False,
        permission=UserPermission.READ_ONLY,
        can_use_api=False,
    )
    db.session.add(user)
    db.session.flush()
    current_app.logger.info(
        'Auto-provisioned SSO user %r from sub=%r', username, sub
    )
    return user, None


def _find_by_match_claim(claim: str, value: str) -> 'User | None':
    """Return a User matched by a single claim, or None if ambiguous/absent."""
    try:
        if claim == 'email':
            return User.query.filter_by(email=value).one()
        # preferred_username (and anything else) → match against username
        return User.query.filter_by(username=value).one()
    except (NoResultFound, MultipleResultsFound):
        return None


def _collect_roles(userinfo: dict) -> frozenset:
    """Collect role strings from standard OIDC claims.

    Checks, in order:
    - realm_access.roles        (Keycloak realm roles)
    - resource_access.<id>.roles (Keycloak client roles)
    - roles                     (generic top-level claim)
    - groups                    (Azure AD / Okta)
    """
    roles: set = set()

    realm_access = userinfo.get('realm_access', {})
    roles.update(realm_access.get('roles', []))

    client_id = current_app.config.get('OIDC_CLIENT_ID', '')
    resource_access = userinfo.get('resource_access', {})
    if client_id and client_id in resource_access:
        roles.update(resource_access[client_id].get('roles', []))

    roles.update(userinfo.get('roles', []))
    roles.update(userinfo.get('groups', []))
    return frozenset(roles)


def _sync_permissions(user: User, userinfo: dict) -> bool:
    """Map OIDC roles to Arcology permissions.  Only updates oidc_managed users.

    Returns True if at least one permission role (admin/read-write/read-only)
    was present in the token, False if none matched.  The caller can use this
    to enforce OIDC_REQUIRE_ROLE.
    """
    if not user.oidc_managed:
        return True  # local accounts are never blocked by role checks

    roles = _collect_roles(userinfo)
    cfg = current_app.config

    admin_role = cfg.get('OIDC_ROLE_ADMIN', 'arcology-admin')
    rw_role    = cfg.get('OIDC_ROLE_READ_WRITE', 'arcology-read-write')
    ro_role    = cfg.get('OIDC_ROLE_READ_ONLY', 'arcology-read-only')
    api_role   = cfg.get('OIDC_ROLE_API_ACCESS', 'arcology-api')

    current_app.logger.debug(
        'OIDC role sync for %r: found roles %r (checking admin=%r rw=%r ro=%r api=%r)',
        user.username, sorted(roles), admin_role, rw_role, ro_role, api_role,
    )

    user.is_admin = admin_role in roles
    user.can_use_api = api_role in roles

    if admin_role in roles or rw_role in roles:
        user.permission = UserPermission.READ_WRITE
        role_matched = True
    elif ro_role in roles:
        user.permission = UserPermission.READ_ONLY
        role_matched = True
    else:
        # No permission role present — demote to minimum rather than leaving
        # the old level in place.  This ensures that removing all Arcology roles
        # from the identity provider actually downgrades the account on next login.
        user.permission = UserPermission.READ_ONLY
        user.is_admin = False
        user.can_use_api = False
        role_matched = False

    current_app.logger.debug(
        'OIDC role sync result for %r: is_admin=%r permission=%r can_use_api=%r role_matched=%r',
        user.username, user.is_admin, user.permission, user.can_use_api, role_matched,
    )
    return role_matched


def _redirect_clearing_provider_session(token: dict):
    """Redirect to the login page after a failed login, clearing the provider session first.

    Without this, users who are already logged into the provider (active SSO session)
    get stuck in a loop: Arcology denies access → redirect to /login → click SSO button
    → provider silently re-authenticates → denied again, forever.

    Only routes through the provider's end_session_endpoint when OIDC_SINGLE_LOGOUT
    is True.  That setting is intended for deployments where the identity provider
    realm is dedicated to Arcology; in a shared corporate SSO realm, clearing the
    provider session would log the user out of unrelated applications.
    """
    if not _bool_cfg(current_app, 'OIDC_SINGLE_LOGOUT'):
        return redirect(url_for('login'))
    try:
        metadata = _oauth.oidc.load_server_metadata()
        end_session = metadata.get('end_session_endpoint')
        if end_session:
            post_logout_uri = url_for('login', _external=True)
            sep = '&' if '?' in end_session else '?'
            target = (
                f'{end_session}{sep}'
                f'post_logout_redirect_uri={urlquote(post_logout_uri, safe="")}'
            )
            id_token = token.get('id_token', '')
            if id_token:
                target += f'&id_token_hint={urlquote(id_token, safe="")}'
            return redirect(target)
    except Exception:
        pass
    return redirect(url_for('login'))

# vim: ts=4 sw=4 et
