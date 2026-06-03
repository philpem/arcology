import sys
import click
from ..database import User
from ..extensions import db


@click.command('set-password')
@click.argument('username')
@click.option('--password', default=None, help='New password (prompted if omitted)')
@click.option('--no-min-length', is_flag=True, default=False,
              help='Bypass the 12-character minimum length check (use for account recovery only)')
def set_password(username, password, no_min_length):
    """Set the local-authentication password for USERNAME.

    Prompts interactively for the new password when --password is not given.
    The 12-character minimum is enforced by default; pass --no-min-length to
    bypass it for emergency account recovery.
    """
    user = User.query.filter_by(username=username).first()
    if user is None:
        raise click.ClickException(f"No user found with username '{username}'.")

    if not password:
        if sys.stdin.isatty():
            password = click.prompt(
                'New password',
                hide_input=True,
                confirmation_prompt='Confirm new password',
            )
        else:
            raise click.ClickException(
                "No --password given and no TTY available for interactive prompt."
            )

    if not no_min_length and len(password) < 12:
        raise click.BadParameter(
            "Password must be at least 12 characters. "
            "Use --no-min-length to bypass this check for account recovery.",
            param_hint="'--password'",
        )

    user.setPassword(password)
    db.session.commit()
    click.echo(f"Password updated for user '{username}'.")

# vim: ts=4 sw=4 et
