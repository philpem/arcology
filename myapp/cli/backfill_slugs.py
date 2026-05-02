import click
from ..database import Artefact, Item
from ..extensions import db
from ..utils.slugs import ensure_unique_slug, generate_slug


@click.command('backfill-slugs')
@click.option('--batch-size', default=500, show_default=True,
              help='Number of rows to commit per batch')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show what would be updated without making changes')
def backfill_slugs(batch_size, dry_run):
    """Populate slug for Items and Artefacts where slug IS NULL.

    Assigns slug = ensure_unique_slug(generate_slug(name/label), ...) for
    every row with a NULL slug.  Items use global uniqueness; Artefacts are
    scoped per item.

    Safe to re-run — rows that already have a slug are skipped.

    Examples:

      docker compose exec web flask backfill-slugs
      docker compose exec web flask backfill-slugs --dry-run
      docker compose exec web flask backfill-slugs --batch-size 200
    """
    dry = dry_run

    # ── Items ────────────────────────────────────────────────────────────────
    items_query = Item.query.filter(Item.slug.is_(None)).order_by(Item.id)
    total_items = items_query.count()
    click.echo(f"Items with NULL slug: {total_items}")

    item_count = 0
    batch = []
    for item in items_query:
        slug = ensure_unique_slug(generate_slug(item.name), Item)
        if dry:
            click.echo(f"  [dry-run] item {item.uuid[:8]} '{item.name}' -> {slug}")
        else:
            item.slug = slug
            batch.append(item)
            if len(batch) >= batch_size:
                db.session.commit()
                batch = []
        item_count += 1

    if not dry and batch:
        db.session.commit()

    click.echo(f"Items: {item_count} slug(s) {'would be ' if dry else ''}assigned.")

    # ── Artefacts ────────────────────────────────────────────────────────────
    artefacts_query = (
        Artefact.query
        .filter(Artefact.slug.is_(None))
        .order_by(Artefact.item_id, Artefact.id)
    )
    total_artefacts = artefacts_query.count()
    click.echo(f"Artefacts with NULL slug: {total_artefacts}")

    artefact_count = 0
    batch = []
    for artefact in artefacts_query:
        slug = ensure_unique_slug(
            generate_slug(artefact.label),
            Artefact,
            scope_filter={'item_id': artefact.item_id},
        )
        if dry:
            click.echo(
                f"  [dry-run] artefact {artefact.uuid[:8]} '{artefact.label}' -> {slug}"
            )
        else:
            artefact.slug = slug
            batch.append(artefact)
            if len(batch) >= batch_size:
                db.session.commit()
                batch = []
        artefact_count += 1

    if not dry and batch:
        db.session.commit()

    click.echo(
        f"Artefacts: {artefact_count} slug(s) {'would be ' if dry else ''}assigned."
    )

# vim: ts=4 sw=4 et
