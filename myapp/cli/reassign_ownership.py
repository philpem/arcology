"""flask reassign-ownership — bulk transfer item/artefact ownership."""
import click
from ..database import Artefact, Item, User
from ..extensions import db


@click.command('reassign-ownership')
@click.option('--from', 'from_username', required=True, metavar='USERNAME|none',
              help='Username whose items and artefacts will be reassigned, or "none" for the unowned pool.')
@click.option('--to', 'to_username', required=True, metavar='USERNAME|none',
              help='Username to receive ownership, or "none" to leave items unowned.')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would change without making any modifications.')
@click.option('--yes', is_flag=True, default=False,
              help='Skip the confirmation prompt.')
def reassign_ownership(from_username, to_username, dry_run, yes):
    """Bulk-reassign all items and artefacts from one owner to another.

    Useful when a curator leaves: transfers their private items and uploaded
    artefacts to a colleague so access and curation work can continue.

    Use "none" as the source to claim all currently unowned items/artefacts.
    Use "none" as the destination to release ownership without a new owner.

    The source user's account is not modified or deleted; remove it separately
    once you are satisfied the reassignment is complete.

    Examples:

      flask reassign-ownership --from alice --to bob
      flask reassign-ownership --from none --to bob
      flask reassign-ownership --from alice --to none --dry-run
      flask reassign-ownership --from alice --to bob --yes
    """
    from_is_unowned = from_username.lower() == 'none'
    to_is_unowned   = to_username.lower() == 'none'

    if from_is_unowned and to_is_unowned:
        raise click.ClickException('Source and destination are both "none" — nothing to do.')

    from_user = None
    if not from_is_unowned:
        from_user = User.query.filter_by(username=from_username).first()
        if not from_user:
            raise click.ClickException(f'User "{from_username}" not found.')

    to_user = None
    if not to_is_unowned:
        to_user = User.query.filter_by(username=to_username).first()
        if not to_user:
            raise click.ClickException(f'User "{to_username}" not found.')
        if from_user and to_user.id == from_user.id:
            raise click.ClickException('Source and destination users are the same.')

    if from_user:
        item_q     = Item.query.filter(Item.owner_id == from_user.id)
        artefact_q = Artefact.query.filter(Artefact.owner_id == from_user.id)
    else:
        item_q     = Item.query.filter(Item.owner_id.is_(None))
        artefact_q = Artefact.query.filter(Artefact.owner_id.is_(None))

    item_count     = item_q.count()
    artefact_count = artefact_q.count()

    from_label = f'"{from_username}"' if from_user else 'unowned pool'
    to_label   = f'"{to_username}"'   if to_user   else 'unowned'

    if item_count == 0 and artefact_count == 0:
        click.echo(f'No items or artefacts found for {from_label} — nothing to do.')
        return

    click.echo(f'Reassigning from {from_label} to {to_label}:')
    click.echo(f'  {item_count} item(s)')
    click.echo(f'  {artefact_count} artefact(s)')

    if dry_run:
        click.echo('Dry run — no changes made.')
        return

    if not yes:
        click.confirm('Proceed?', abort=True)

    new_owner_id = to_user.id if to_user else None
    item_q.update({'owner_id': new_owner_id})
    artefact_q.update({'owner_id': new_owner_id})
    db.session.commit()

    click.echo(f'Done. {item_count} item(s) and {artefact_count} artefact(s) reassigned.')

# vim: ts=4 sw=4 et
