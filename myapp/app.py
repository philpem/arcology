from __future__ import absolute_import, unicode_literals

from flask_bootstrap import Bootstrap5
from flask import Flask, render_template, redirect, request, url_for, flash, g
from flask_login import (LoginManager, current_user, login_required,
        login_user, logout_user, confirm_login, fresh_login_required)
from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField, StringField
from wtforms.validators import DataRequired
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
import click
import os
import secrets
import sys
from urllib.parse import urlparse

from .extensions import db, migrate, login_manager, bootstrap, csrf
from .database import UserPermission, ApiKeyPermission

# Subclass the application so we can add the menu management functions
class AppClass(Flask):
    def __init__(self, *args, **kwargs):
        # Let Flask initialise itself
        super(AppClass, self).__init__(*args, **kwargs)
        # Create an empty menu list
        self._myapp_menudata = list()

    def add_menu_item(self, label, endpoint, sortorder=0):
        """ Add a menu item to the application menu """
        self._myapp_menudata.append(dict(label=label, endpoint=endpoint, sortorder=sortorder))

def create_app(config_name=None):
    # create and configure the application
    app = AppClass(__name__)
    app.config.from_pyfile(config_name or 'myapp.cfg', silent=True)

    # Load settings from environment, overriding config file where set
    for env_key in ('SECRET_KEY', 'SQLALCHEMY_DATABASE_URI', 'WORKER_API_KEY'):
        env_val = os.environ.get(env_key)
        if env_val:
            app.config[env_key] = env_val

    # Abort if no database URI is configured
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        raise RuntimeError(
            "SQLALCHEMY_DATABASE_URI is not set — configure it in myapp.cfg or as an environment variable"
        )

    # Warn if WORKER_API_KEY is missing
    if not app.config.get('WORKER_API_KEY'):
        app.logger.warning("WORKER_API_KEY is not configured — worker API authentication will fail")

    # Warn and auto-generate SECRET_KEY if missing, left at the default placeholder, or too short
    secret_key = app.config.get('SECRET_KEY', '')
    if not secret_key or secret_key in ['0123456789ABCDEF', 'CHANGE_ME'] or len(secret_key) < 32:
        app.logger.warning("!!! SECRET_KEY not set, left at default, or too short - generating random key for this session")
        app.logger.warning("!!! Sessions will be lost on server restart - set SECRET_KEY in myapp.cfg or as an environment variable")
        app.config['SECRET_KEY'] = secrets.token_urlsafe(32)

    # Initialise extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    bootstrap.init_app(app)
    csrf.init_app(app)

    # Flask-Login configuration
    login_manager.login_view = 'login'
    login_manager.login_message = u'Please log in to access this page.'

    # tell jinja to remove extraneous whitespace
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    # Add custom Jinja2 filters
    import json
    app.jinja_env.filters['fromjson'] = lambda s: json.loads(s) if s else {}

    # RISC OS filetype formatting
    from .riscos_filetypes import format_filetype, get_filetype_name
    app.jinja_env.filters['format_filetype'] = format_filetype
    app.jinja_env.filters['filetype_name'] = get_filetype_name

    # enable database logging (if enabled)
    if app.config.get('DEBUG_DB_LOG', False):
        from flask.logging import default_handler
        import logging
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
        """Inject user_can_write into every template context."""
        can_write = (current_user.is_authenticated and
                     current_user.has_permission(UserPermission.READ_WRITE))
        return dict(user_can_write=can_write)

    # Register login handlers, error handlers, blueprints, and CLI commands
    _register_login_handlers(app)
    _register_error_handlers(app)
    _register_blueprints(app)
    _register_cli_commands(app)

    return app


def _register_blueprints(app):
    """ Load and register all blueprints from the 'blueprints' directory. """
    import pkgutil
    from . import blueprints

    for importer,modname,ispkg in pkgutil.iter_modules(blueprints.__path__):
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

def _register_login_handlers(app):
    # -- login management --

    from .database import User

    # TODO -- For 'fresh_login_required' to work, we need a "reauthenticate" handler.
    #   See https://github.com/maxcountryman/flask-login/blob/master/example/login-example.py for a code example
    #login_manager.refresh_view = 'reauth'
    #login_manager.needs_refresh_message = u'To protect your account, please re-authenticate to access this page.'

    @login_manager.user_loader
    def load_user(userid):
        userrec = None
        try:
            userrec = User.query.filter(User.id == int(userid)).one()
        except MultipleResultsFound:
            app.logger.error("USER LOGIN FAILURE: User '%s' has a doppelganger (duplicate username found)")
        except NoResultFound:
            app.logger.warning("Userloader: id '%d' returned no results" % int(userid))
            pass # with userrec = None
        return userrec

    @app.route("/login", methods=["GET","POST"])
    def login():
        class LoginForm(FlaskForm):
            username=StringField("Username", validators=[DataRequired()])
            password=PasswordField("Password", validators=[DataRequired()])
            submit=SubmitField("Log in")

        form = LoginForm()
        if form.validate_on_submit():
            # login and validate the user
            userrec = None
            try:
                userrec = User.query.filter(User.username == form.username.data).one()
            except MultipleResultsFound:
                app.logger.error("USER LOGIN FAILURE: User '%s' has a doppelganger (duplicate username found)")
            except NoResultFound:
                pass # with userrec = None

            if userrec is not None:
                # check password
                if userrec.checkPassword(form.password.data):
                    login_user(userrec)
                    #flash("Logged in successfully", "success")
                    # Redirect to the page the user was trying to reach, or the dashboard.
                    # SECURITY: reject any next= URL with a netloc (host) component to
                    # prevent open-redirect attacks that send users to external sites.
                    next_url = request.args.get("next")
                    if next_url and urlparse(next_url).netloc != '':
                        next_url = None
                    return redirect(next_url or url_for("myapp_blueprints_dashboard.index"))

        if request.method == 'POST':
            flash("Error logging in - please check your username and password and ensure that CAPS LOCK is turned off.", "error")
        return render_template("login.html", form=form)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have now been logged out.", "info")
        return redirect(url_for("login"))

def _register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(e):
        return render_template('errors/500.html'), 500


def _register_cli_commands(app):
    from .database import User

    @app.cli.command('create-admin')
    @click.option('--username', default=None, help='Admin username (or set ADMIN_USERNAME env var)')
    @click.option('--password', default=None, help='Admin password (or set ADMIN_PASSWORD env var)')
    def create_admin(username, password):
        """Create an administrator user account.

        Credentials are taken from --username/--password options, or from the
        ADMIN_USERNAME/ADMIN_PASSWORD environment variables. If neither is
        provided and a TTY is available the command prompts interactively.

        The command is idempotent: it exits without error if any user already
        exists. Run it again with --username to add a second admin.
        """
        # Idempotency: skip if any user exists already
        if User.query.first() is not None:
            click.echo("Admin user already exists — skipping.")
            return

        # Resolve credentials: flag > env var > interactive prompt / give up
        username = username or os.environ.get('ADMIN_USERNAME')
        password = password or os.environ.get('ADMIN_PASSWORD')

        has_tty = sys.stdin.isatty()

        if not username:
            if has_tty:
                username = click.prompt('Admin username')
            else:
                click.echo(
                    "WARNING: No admin user created. "
                    "Set ADMIN_USERNAME/ADMIN_PASSWORD env vars, or run "
                    "'flask create-admin' interactively.",
                    err=True,
                )
                return

        if not password:
            if has_tty:
                password = click.prompt(
                    'Admin password',
                    hide_input=True,
                    confirmation_prompt='Confirm password',
                )
            else:
                click.echo(
                    "WARNING: No admin user created. "
                    "Set ADMIN_USERNAME/ADMIN_PASSWORD env vars, or run "
                    "'flask create-admin' interactively.",
                    err=True,
                )
                return

        if len(password) < 12:
            raise click.BadParameter(
                "Password must be at least 12 characters.",
                param_hint="'--password'",
            )

        user = User()
        user.username = username
        user.setPassword(password)
        user.is_admin = True
        user.permission = UserPermission.READ_WRITE
        user.can_use_api = True
        db.session.add(user)
        db.session.commit()
        click.echo(f"Admin user '{username}' created successfully.")

	@app.cli.command('backfill-search')
	def backfill_search():
		"""Populate search index tables from existing completed analyses.

		Reads all completed DISC_PROTECTION_DETECT, DISC_MASTERING_DETECT,
		and PARTITION_DETECT analyses and writes structured rows to:
		  - artefact_protection
		  - artefact_mastering
		  - partitions.gnu_file_type

		The command is idempotent: existing rows are replaced on each run.
		Run this once after applying the b2e8f4a1c9d3 migration. It is also
		called automatically by the Docker entrypoint on every container start
		(safe because completed analyses are not re-processed from scratch).
		"""
		import json
		from .database import (
			Analysis, AnalysisType, AnalysisStatus,
			Partition, ArtefactProtection, ArtefactMastering,
		)

		prot_count = 0
		mast_count = 0
		part_count = 0

		# DISC_PROTECTION_DETECT
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		click.echo(f"Processing {len(analyses)} DISC_PROTECTION_DETECT analyses...")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				click.echo(f"  WARNING: could not parse details for analysis {analysis.uuid}", err=True)
				continue
			ArtefactProtection.query.filter_by(artefact_id=analysis.artefact_id).delete()
			for ind in details.get('indicators', []):
				db.session.add(ArtefactProtection(
					artefact_id=analysis.artefact_id,
					protection_type=ind.get('type', 'unknown'),
					track=ind.get('track'),
					side=ind.get('side'),
					details=ind.get('sector_id') or ind.get('details'),
				))
				prot_count += 1

		# DISC_MASTERING_DETECT
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.DISC_MASTERING_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		click.echo(f"Processing {len(analyses)} DISC_MASTERING_DETECT analyses...")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				click.echo(f"  WARNING: could not parse details for analysis {analysis.uuid}", err=True)
				continue
			ArtefactMastering.query.filter_by(artefact_id=analysis.artefact_id).delete()
			for ind in details.get('indicators', []):
				db.session.add(ArtefactMastering(
					artefact_id=analysis.artefact_id,
					mastering_type=ind.get('type', 'unknown'),
					track=ind.get('track'),
					decoded=ind.get('decoded') or ind.get('data'),
				))
				mast_count += 1

		# PARTITION_DETECT → gnu_file_type
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.PARTITION_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		click.echo(f"Processing {len(analyses)} PARTITION_DETECT analyses...")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				click.echo(f"  WARNING: could not parse details for analysis {analysis.uuid}", err=True)
				continue
			gnu_file_type = details.get('file', {}).get('file_type')
			if gnu_file_type:
				updated = (
					Partition.query
					.filter_by(artefact_id=analysis.artefact_id)
					.update({'gnu_file_type': gnu_file_type})
				)
				part_count += updated

		db.session.commit()
		click.echo(
			f"Done. Protection indicators: {prot_count}, "
			f"mastering indicators: {mast_count}, "
			f"partitions updated: {part_count}."
		)


# vim: ts=4 sw=4 et
