import os
import sys
import click
from ..extensions import db
from ..database import User, UserPermission


@click.command('create-admin')
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

# vim: ts=4 sw=4 et
