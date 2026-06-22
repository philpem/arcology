import click
from flask import current_app
from arcology_shared.enums import ArtefactType
from arcology_shared.fuzzyhash import HAS_TLSH, compute_tlsh_stream
from ..database import Artefact, db
from ..services.artefact_storage import get_artefact_storage_key

# Flux types are skipped: their raw bytes carry timing noise (see the worker's
# CHECKSUM_COMPUTE handler).
_FLUX_TYPES = {ArtefactType.SCP, ArtefactType.DFI, ArtefactType.A2R}

# Emit a progress line every this many artefacts scanned.  Each artefact's blob
# is streamed (and can be multi-GB), so this is far smaller than the file-level
# rescan's threshold.
_PROGRESS_EVERY = 100


@click.command('backfill-tlsh')
@click.option('--force', is_flag=True, help='Recompute even if a tlsh is already set')
@click.option('--batch-size', default=100, show_default=True,
              help='Number of artefacts to process per database commit')
def backfill_tlsh(force, batch_size):
    """Compute artefact-level TLSH fuzzy hashes for existing artefacts.

    Streams each (non-flux) artefact blob and stores its TLSH digest, for
    artefacts uploaded before TLSH was added. ExtractedFile TLSH cannot be
    backfilled without re-extraction — it populates going forward.

      docker compose exec web flask backfill-tlsh
    """
    if not HAS_TLSH:
        click.echo("ERROR: py-tlsh is not installed; cannot compute TLSH.", err=True)
        raise SystemExit(1)

    q = Artefact.query.filter(Artefact.artefact_type.notin_(_FLUX_TYPES))
    if not force:
        q = q.filter(Artefact.tlsh.is_(None))

    total = q.count()
    click.echo(f"  {total} artefact(s) to hash …")
    scanned = updated = pending = 0
    for art in q.yield_per(batch_size):
        if art.artefact_type in _FLUX_TYPES:
            continue
        scanned += 1
        try:
            key = get_artefact_storage_key(art)
            with current_app.storage.open_read(key) as fh:
                digest = compute_tlsh_stream(fh)
        except Exception as e:  # noqa: BLE001 - report and continue
            click.echo(f"  skip {art.uuid}: {e}", err=True)
            continue
        if digest:
            art.tlsh = digest
            updated += 1
            pending += 1
            if pending >= batch_size:
                db.session.commit()
                pending = 0
        if scanned % _PROGRESS_EVERY == 0:
            pct = f" ({scanned * 100 // total}%)" if total else ""
            click.echo(f"  {scanned}/{total} scanned{pct}, {updated} hashed")
    db.session.commit()
    click.echo(f"Done. {updated} of {scanned} artefact(s) updated.")

# vim: ts=4 sw=4 et
