
import os
import secrets
from urllib.parse import urlsplit
from flask import Flask, g, render_template, request, url_for
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix
from .database import UserPermission
from .extensions import bootstrap, csrf, db, login_manager, migrate


def _s3_public_origin(config):
    """Return the ``scheme://host[:port]`` origin browsers load S3 outputs from.

    Only relevant when S3 storage is active and a browser-reachable URL is
    configured.  Prefers ``S3_PUBLIC_URL`` (the public, browser-facing host) and
    falls back to ``S3_ENDPOINT_URL``.  Returns ``None`` when not using S3, when
    no URL is configured, or when the URL cannot be parsed into an origin — in
    which case the CSP is left unchanged.

    Only the origin is returned (no path/query): CSP source expressions match on
    origin, so this whitelists every pre-signed object URL on that host.
    """
    if str(config.get('STORAGE_BACKEND', 'local')).lower() != 's3':
        return None
    url = config.get('S3_PUBLIC_URL') or config.get('S3_ENDPOINT_URL')
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


# Subclass the application so we can add the menu management functions
class AppClass(Flask):
    def __init__(self, *args, **kwargs):
        # Let Flask initialise itself
        super().__init__(*args, **kwargs)
        # Create an empty menu list
        self._myapp_menudata = list()

    def add_menu_item(self, label, endpoint, sortorder=0):
        """ Add a menu item to the application menu """
        self._myapp_menudata.append(dict(label=label, endpoint=endpoint, sortorder=sortorder))

def create_app(config_name=None):
    # create and configure the application
    app = AppClass(__name__)
    # Trust X-Forwarded-* headers from one upstream proxy (nginx, Caddy, Traefik, etc.)
    # so that url_for(_external=True) and OIDC redirect URIs use the public hostname/scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config.from_pyfile(config_name or 'myapp.cfg', silent=True)

    # Load settings from environment, overriding config file where set
    for env_key in ('SECRET_KEY', 'SQLALCHEMY_DATABASE_URI', 'WORKER_API_KEY',
                    'STORAGE_BACKEND', 'S3_ENDPOINT_URL', 'S3_BUCKET',
                    'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_REGION',
                    'S3_PUBLIC_URL',
                    'OIDC_ENABLED', 'LOCAL_LOGIN_ENABLED', 'OIDC_PROVIDER_NAME',
                    'OIDC_DISCOVERY_URL', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET',
                    'OIDC_SCOPES', 'OIDC_MATCH_CLAIM',
                    'OIDC_ROLE_ADMIN', 'OIDC_ROLE_READ_WRITE', 'OIDC_ROLE_READ_ONLY',
                    'OIDC_ROLE_STAFF', 'OIDC_ROLE_API_ACCESS', 'OIDC_REQUIRE_ROLE',
                    'OIDC_SINGLE_LOGOUT', 'OIDC_SYNC_INTERVAL', 'OIDC_AUTO_REDIRECT',
                    'PUBLIC_MODE', 'PUBLIC_DOWNLOADS',
                    'JINJA_BYTECODE_CACHE', 'JINJA_BYTECODE_CACHE_DIR', 'JINJA_PREWARM',
                    'SENTRY_DSN'):
        env_val = os.environ.get(env_key)
        if env_val:
            app.config[env_key] = env_val

    # Integer env vars — loaded separately so they're stored as int, not str.
    for int_key in ('WEB_UI_ANALYSIS_PRIORITY', 'STALE_JOB_TIMEOUT_SECONDS'):
        env_val = os.environ.get(int_key)
        if env_val is not None:
            try:
                app.config[int_key] = int(env_val)
            except ValueError:
                app.logger.warning(f'{int_key} env var is not an integer: {env_val!r}')

    # Float env vars — loaded separately so they're stored as float, not str.
    for float_key in ('SENTRY_TRACES_SAMPLE_RATE', 'SENTRY_PROFILES_SAMPLE_RATE'):
        env_val = os.environ.get(float_key)
        if env_val is not None:
            try:
                app.config[float_key] = float(env_val)
            except ValueError:
                app.logger.warning(f'{float_key} env var is not a number: {env_val!r}')

    # Abort if no database URI is configured
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        raise RuntimeError(
            "SQLALCHEMY_DATABASE_URI is not set — configure it in myapp.cfg or as an environment variable"
        )

    # Warn if WORKER_API_KEY is missing
    if not app.config.get('WORKER_API_KEY'):
        app.logger.warning("WORKER_API_KEY is not configured — worker API authentication will fail")

    # Validate WEB_UI_ANALYSIS_PRIORITY when explicitly configured
    web_priority = app.config.get('WEB_UI_ANALYSIS_PRIORITY')
    if web_priority is not None and web_priority < 0:
        raise ValueError(
            f"WEB_UI_ANALYSIS_PRIORITY must be >= 0 (got {web_priority!r}); "
            "a negative value would demote web UI jobs behind API/CLI jobs"
        )

    # Warn and auto-generate SECRET_KEY if missing, left at the default placeholder, or too short
    secret_key = app.config.get('SECRET_KEY', '')
    if not secret_key or secret_key in ['0123456789ABCDEF', 'CHANGE_ME'] or len(secret_key) < 32:
        app.logger.warning("!!! SECRET_KEY not set, left at default, or too short - generating random key for this session")
        app.logger.warning("!!! Sessions will be lost on server restart - set SECRET_KEY in myapp.cfg or as an environment variable")
        app.config['SECRET_KEY'] = secrets.token_urlsafe(32)

    # Connection pool tuning: allow enough connections for Gunicorn workers
    # under concurrent load.  Defaults can be overridden in myapp.cfg or env.
    # Only applies to PostgreSQL; SQLite uses StaticPool which doesn't support these.
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if 'SQLALCHEMY_ENGINE_OPTIONS' not in app.config and db_uri.startswith('postgresql'):
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'pool_size': 10,
            'max_overflow': 20,
            'pool_recycle': 1800,
            'pool_pre_ping': True,
        }

    # Initialise Sentry
    if 'SENTRY_DSN' in app.config:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        sentry_sdk.init(
                dsn=app.config['SENTRY_DSN'],
                integrations=[FlaskIntegration(), SqlalchemyIntegration()],
                traces_sample_rate=app.config.get('SENTRY_TRACES_SAMPLE_RATE', 1.0),
                profiles_sample_rate=app.config.get('SENTRY_PROFILES_SAMPLE_RATE', 1.0),
                send_default_pii=True,
                )

    # Initialise extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    bootstrap.init_app(app)
    csrf.init_app(app)

    # Initialise storage backend (local filesystem or S3-compatible)
    from shared.storage import create_storage
    storage_config = dict(app.config)
    # Resolve relative folder paths to absolute for local storage
    upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
    output_folder = app.config.get('OUTPUT_FOLDER', 'outputs')
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(app.instance_path, upload_folder)
    if not os.path.isabs(output_folder):
        output_folder = os.path.join(app.instance_path, output_folder)
    storage_config['UPLOAD_FOLDER'] = upload_folder
    storage_config['OUTPUT_FOLDER'] = output_folder
    app.storage = create_storage(storage_config)

    # Flask-Login configuration (login_view is set by the auth blueprint init_app)
    login_manager.login_message = 'Please log in to access this page.'

    # tell jinja to remove extraneous whitespace
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    # Persist compiled template bytecode to disk so a freshly-started worker
    # does not pay the lex/parse/codegen cost on the first render of each
    # template (the dominant cost of cold-worker page loads — issue #447).
    # Disable with JINJA_BYTECODE_CACHE=false.
    if str(app.config.get('JINJA_BYTECODE_CACHE', True)).lower() not in ('false', '0', 'no'):
        from jinja2 import FileSystemBytecodeCache
        cache_dir = app.config.get('JINJA_BYTECODE_CACHE_DIR') or \
            os.path.join(app.instance_path, 'jinja_cache')
        try:
            os.makedirs(cache_dir, exist_ok=True)
            app.jinja_env.bytecode_cache = FileSystemBytecodeCache(cache_dir)
        except OSError as e:
            app.logger.warning(f'Could not enable Jinja bytecode cache at {cache_dir}: {e}')

    # Add custom Jinja2 filters
    import json
    app.jinja_env.filters['fromjson'] = lambda s: json.loads(s) if s else {}

    # Safe enum value access — tolerant of None from _TolerantEnum (orphan rows).
    # Used by templates that render artefact_type/analysis_type so an orphan row
    # shows a fallback label instead of crashing the page with a 500.
    from .utils.enum_display import enum_value
    app.jinja_env.filters['enum_value'] = enum_value

    # RISC OS filetype formatting
    from .riscos_filetypes import format_filetype, get_filetype_name
    app.jinja_env.filters['format_filetype'] = format_filetype
    app.jinja_env.filters['filetype_name'] = get_filetype_name

    # Extension-based filetype labels (non-RISC OS files)
    from .extension_labels import extension_label, unified_type_label
    app.jinja_env.filters['extension_label'] = extension_label
    app.jinja_env.filters['unified_type_label'] = unified_type_label

    # Analysis type display names — handles cases where the default
    # value.replace('_', ' ').title() produces wrong capitalisation.
    _ANALYSIS_TYPE_DISPLAY = {
        'format_identify':    'File Format Identify',
        'riscos_module_parse': 'RISC OS Module parse',
    }
    def _format_analysis_type(value):
        """Format an analysis type enum value for display."""
        s = value.value if hasattr(value, 'value') else str(value)
        return _ANALYSIS_TYPE_DISPLAY.get(s, s.replace('_', ' ').title())
    app.jinja_env.filters['format_analysis_type'] = _format_analysis_type

    def _format_filesize(size_bytes):
        """Format a byte count as a human-readable size with the most appropriate unit."""
        if size_bytes is None:
            return '-'
        size = float(size_bytes)
        for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
            if abs(size) < 1024.0 or unit == 'TB':
                return f'{int(size)} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024.0
    app.jinja_env.filters['format_filesize'] = _format_filesize

    # enable database logging (if enabled)
    if app.config.get('DEBUG_DB_LOG', False):
        import logging
        from flask.logging import default_handler
        app.logger.warning('Warning - database logging enabled. This will spam the logs!')
        logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
        logging.getLogger('sqlalchemy.engine').addHandler(default_handler)

    # start database profiling (if enabled)
    if app.config.get('DEBUG_DB_PROFILING', False):
        app.logger.warning('Warning - database profiling enabled. Do not use this in production!')
        try:
            import sqltap
        except ImportError:
            app.logger.error('Cannot import sqltap. Install it with pip to use profiling!')
            raise

        def context_fn(*args):
            import uuid
            try:
                return g.req_id
            except AttributeError:
                g.req_id = uuid.uuid4().hex
                return g.req_id

        # -- post-request teardown --
        @app.teardown_request
        def shutdown_session(exception=None):
            # if database profiling is enabled, save a report
            if app.config.get('DEBUG_DB_PROFILING', False) and not request.path.startswith('/static'):
                # filter out any statistics which aren't for this request
                stats_all = sqltap_sess.collect()
                stats_req = list(filter(lambda x: x.user_context == g.req_id, stats_all))
                sqltap.report(stats_all, os.path.join(os.path.dirname(os.path.realpath(__file__)), "static/db_profile_report_all.html"))
                sqltap.report(stats_req, os.path.join(os.path.dirname(os.path.realpath(__file__)), "static/db_profile_report_req.html"))

        sqltap_sess = sqltap.start(user_context_fn = context_fn)

    # -- main menu handing (context processor) --
    @app.context_processor
    def inject_menu():
        """
        Context processor for the main menu

        Adds the main menu data into the template context. Menu items are sorted by sort-order, then (case-insensitively) by label.
        """
        return dict(menu=sorted(app._myapp_menudata,
                                key=lambda mi: (mi['sortorder'], mi['label'].lower())))

    # -- user permission context processor --
    @app.context_processor
    def inject_user_permissions():
        """Inject user_can_write, public_mode, and public_downloads into every template context."""
        from .permissions import _bool_config
        can_write = (current_user.is_authenticated and
                     current_user.has_permission(UserPermission.READ_WRITE))
        pm = _bool_config('PUBLIC_MODE')
        pd = _bool_config('PUBLIC_DOWNLOADS', default=True)
        return dict(user_can_write=can_write, public_mode=pm, public_downloads=pd)

    # -- version context processor --
    import datetime
    from .version import get_version
    @app.context_processor
    def inject_version():
        """Inject app_version and now into every template context."""
        return dict(app_version=get_version(), now=datetime.datetime.now())

    # -- template filter: analysis status -> Bootstrap badge class --
    @app.template_filter('status_badge_class')
    def status_badge_class(status_value):
        """Return a Bootstrap bg-* class for a given analysis status string."""
        return {
            'completed': 'bg-success',
            'failed': 'bg-danger',
            'running': 'bg-info',
        }.get(status_value, 'bg-warning')

    # -- template global: generate the canonical URL for an artefact action endpoint --
    @app.template_global('artefact_url')
    def artefact_url(artefact, endpoint='view', **kwargs):
        """Return the canonical URL for an artefact action endpoint.

        Root artefacts:    /items/<item_id>/artefacts/<artefact_id>/<action>
        Derived artefacts: /items/<item_id>/artefacts/<root_id>/<artefact_id>/<action>

        Derived artefacts use the ``_nested`` endpoint variant so that
        url_for() generates the two-segment path rather than appending
        root_id as a query parameter.
        """
        root = artefact.root_artefact
        if root is not artefact:
            route = 'myapp_blueprints_artefacts.' + endpoint + '_nested'
            kw = {'item_id': artefact.item.url_id, 'root_id': root.url_slug, 'artefact_id': artefact.url_slug}
        else:
            route = 'myapp_blueprints_artefacts.' + endpoint
            kw = {'item_id': artefact.item.url_id, 'artefact_id': artefact.url_slug}
        kw.update(kwargs)
        return url_for(route, **kw)

    # Content-Security-Policy header.
    # Allows Bootstrap CSS/JS/Icons fonts from cdn.jsdelivr.net plus inline
    # styles and scripts used throughout the templates.  Set CSP_HEADER to an
    # empty string in config to disable (e.g. when the reverse proxy sets it).
    #
    # When S3 storage is configured with a browser-reachable public URL on a
    # different origin (S3_PUBLIC_URL, e.g. https://arco-s3.example.com), output
    # files are served by redirecting <img> requests to pre-signed URLs on that
    # origin.  Browsers re-check CSP against each redirect hop, so the S3 origin
    # must be whitelisted in img-src/media-src or every visualisation is blocked.
    _s3_origin = _s3_public_origin(app.config)
    _img_src = "'self' data:"
    _media_src = "'self'"
    if _s3_origin:
        _img_src = f"{_img_src} {_s3_origin}"
        _media_src = f"{_media_src} {_s3_origin}"
    _DEFAULT_CSP = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net; "
        f"img-src {_img_src}; "
        f"media-src {_media_src}; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'"
    )

    @app.after_request
    def set_csp(response):
        csp = app.config.get('CSP_HEADER', _DEFAULT_CSP)
        if csp:
            response.headers['Content-Security-Policy'] = csp
        return response

    # Register error handlers, blueprints, and CLI commands
    _register_error_handlers(app)
    _register_blueprints(app)
    _register_cli_commands(app)

    # Compile all templates at startup (after blueprints register their template
    # folders) so the lex/parse/codegen cost is paid once during worker boot
    # rather than on the first user request to hit each template — the dominant
    # cost of cold-worker page loads (#447). Disable with JINJA_PREWARM=false.
    if str(app.config.get('JINJA_PREWARM', True)).lower() not in ('false', '0', 'no'):
        _prewarm_templates(app)

    return app


def _prewarm_templates(app):
    """Pre-compile every template so no user request pays compilation cost.

    Compilation only parses and generates code (no rendering), so loading every
    template is side-effect free. Failures are logged and skipped so a single
    unparseable template never blocks startup.
    """
    compiled = 0
    try:
        names = app.jinja_env.list_templates()
    except Exception as e:  # loader may not support listing
        app.logger.warning(f'Template prewarm skipped (cannot list templates): {e}')
        return
    for name in names:
        try:
            app.jinja_env.get_template(name)
            compiled += 1
        except Exception as e:
            app.logger.debug(f'Template prewarm skipped {name}: {e}')
    app.logger.info(f'Prewarmed {compiled}/{len(names)} templates')


def _register_blueprints(app):
    """ Load and register all blueprints from the 'blueprints' directory. """
    import pkgutil
    from . import blueprints

    for _importer, modname, _ispkg in pkgutil.iter_modules(blueprints.__path__):
        try:
            module = __import__(f"myapp.blueprints.{modname}", fromlist=[modname])
            if hasattr(module, 'blueprint'):
                app.register_blueprint(module.blueprint)
                app.logger.info(f"Registered blueprint: {modname}")
            else:
                app.logger.warning(f"Module {modname} has no 'blueprint' attribute")

            # call init_app if the module provides it
            if hasattr(module, 'init_app'):
                module.init_app(app)

        except Exception as e:
            app.logger.error(f"Failed to load blueprint {modname}: {e}", exc_info=True)
            continue

def _register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(e):
        return render_template('errors/500.html'), 500


def _register_cli_commands(app):
    from .cli.backfill_blobs import backfill_blobs
    from .cli.backfill_slugs import backfill_slugs
    from .cli.cancel_analysis import cancel_analysis
    from .cli.create_admin import create_admin
    from .cli.dedup_artefacts import dedup_artefacts
    from .cli.reanalyse import reanalyse
    from .cli.reassign_ownership import reassign_ownership
    from .cli.rebuild_search_index import rebuild_search_index
    from .cli.rescan_hashes import rescan_hashes
    from .cli.set_password import set_password

    app.cli.add_command(create_admin)
    app.cli.add_command(rebuild_search_index)
    app.cli.add_command(rescan_hashes)
    app.cli.add_command(reanalyse)
    app.cli.add_command(cancel_analysis)
    app.cli.add_command(dedup_artefacts)
    app.cli.add_command(reassign_ownership)
    app.cli.add_command(backfill_slugs)
    app.cli.add_command(backfill_blobs)
    app.cli.add_command(set_password)


# vim: ts=4 sw=4 et
