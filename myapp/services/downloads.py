"""Arcology - Download serving service

Shared serving tails for artefact downloads, extracted-file downloads, and
analysis output files.  The web blueprint and the REST API apply their own
restriction/visibility enforcement (flash + redirect vs JSON 403) and then
delegate the actual serving — presigned-URL redirect in S3 mode, traversal-
checked send_file in local mode — to these helpers, so the two stacks cannot
drift apart.

Each helper returns a Flask response, or None when the underlying file does
not exist; callers translate None into their own 404 shape.
"""

import mimetypes
import os
from flask import current_app, redirect, send_file
# Imported for its import-time side effect: arcology_shared/storage.py is the
# canonical spot (per CLAUDE.md) that registers media Content-Types via
# mimetypes.add_type, and nothing else imports it eagerly (artefact_storage
# imports it lazily), so this guarantees those types are registered before we
# serve any local file with mimetypes.guess_type().
from arcology_shared import storage as _storage  # noqa: F401
from arcology_shared.transcode_paths import CONTENT_ADDRESSED_MEDIA_PREFIX
from ..database import Artefact
from .artefact_storage import (
    get_artefact_path,
    get_artefact_storage_key,
    resolve_extracted_file_path,
)

# Length of a UUID hex string as used in output paths.
UUID_HEX_LEN = 32


def serve_artefact_file(artefact, inline=False):
    """Serve an artefact's stored file.

    S3 mode: redirect to a pre-signed URL.  Local mode: send_file (None when
    the file is missing on disk).  Callers must have already enforced
    visibility and download restrictions.

    With ``inline=True`` the file is served for in-page playback rather than as
    an attachment download (no Content-Disposition: attachment, explicit
    Content-Type) — used by the media player for browser-playable artefacts.
    """
    storage = current_app.storage
    key = get_artefact_storage_key(artefact)

    # In inline mode, presign WITHOUT a filename so no Content-Disposition:
    # attachment is set (it would force a download instead of in-page playback);
    # the presigned URL still carries the right Content-Type from the key.
    url = storage.presigned_url(key, filename=None if inline else artefact.original_filename)
    if url:
        return redirect(url)

    full_path = get_artefact_path(artefact)
    if not os.path.exists(full_path):
        return None
    if inline:
        mime, _ = mimetypes.guess_type(artefact.original_filename or full_path)
        return send_file(full_path, mimetype=mime or 'application/octet-stream')
    return send_file(
        full_path,
        as_attachment=True,
        download_name=artefact.original_filename,
    )


def serve_extracted_file(ef, inline=False):
    """Serve an extracted file.

    Resolves the file's on-disk location via the extraction outputs (handles
    S3 by downloading to a temp file).  Returns None when the file cannot be
    found.  Callers must have already enforced visibility and restrictions.

    With ``inline=True`` the file is served for in-page playback (explicit
    Content-Type, no attachment disposition, byte-range support via send_file)
    rather than as a download — used by the media player for browser-playable
    extracted media.
    """
    file_path = resolve_extracted_file_path(ef)
    if not file_path:
        return None
    if inline:
        mime, _ = mimetypes.guess_type(ef.filename or file_path)
        return send_file(file_path, mimetype=mime or 'application/octet-stream')
    return send_file(file_path, as_attachment=True, download_name=ef.filename)


def resolve_output_artefact(filename):
    """Resolve the artefact that owns an analysis output path.

    Output paths follow ``{item_part}/{artefact_uuid}_{slug}/{file...}`` —
    the artefact UUID is the part of the second path component before the
    first underscore.  This is the single place that layout assumption is
    encoded; both the web and API output routes use it to enforce artefact
    visibility before serving.

    Returns the Artefact, or None when the path does not match the layout or
    no such artefact exists.
    """
    path_parts = filename.split('/', 2)
    if len(path_parts) < 2:
        return None
    uuid_candidate = path_parts[1].split('_', 1)[0]
    if len(uuid_candidate) != UUID_HEX_LEN:
        return None
    return Artefact.query.filter_by(uuid=uuid_candidate).first()


# Content-addressed, shared transcode outputs (see the worker's
# transcode_output_subdir): keyed on the source media's hash, not on any one
# artefact, so they live under this prefix instead of outputs/{item}/{artefact}/.
# The prefix is defined once in arcology_shared so the worker (writer) and the
# web app (this resolver) cannot drift.
CONTENT_ADDRESSED_OUTPUT_PREFIX = CONTENT_ADDRESSED_MEDIA_PREFIX


def resolve_output_artefacts(filename):
    """Return every artefact entitled to view the output at *filename* (0+).

    Legacy per-artefact outputs embed the owning artefact's UUID in the path and
    resolve to that single artefact.  Content-addressed transcode outputs
    (``media/{sha256}/{ver}/...``) are shared by every artefact that holds
    byte-identical source media, so they are reverse-looked-up via the
    ReplayMovie / MediaFile rows referencing them and may have several owners.
    """
    if (filename or '').startswith(CONTENT_ADDRESSED_OUTPUT_PREFIX):
        from ..database import MediaFile, ReplayMovie
        from ..extensions import db
        owner_ids: set[int] = set()
        for model in (ReplayMovie, MediaFile):
            owner_ids.update(db.session.scalars(
                db.select(model.artefact_id).where(db.or_(
                    model.mp4_output_path == filename,
                    model.poster_path == filename,
                ))
            ).all())
        if not owner_ids:
            return []
        return list(db.session.scalars(
            db.select(Artefact).where(Artefact.id.in_(owner_ids))
        ).all())
    artefact = resolve_output_artefact(filename)
    return [artefact] if artefact is not None else []


def output_access_decision(filename, user, *, sees_all=False):
    """Authorisation decision for serving the output at *filename*.

    Returns ``'not_found'`` (no entitled artefact is viewable — 404; a private
    artefact's outputs must be indistinguishable from nonexistent),
    ``'restricted'`` (viewable but every entitled artefact is download-restricted
    for this user — 403), or ``'ok'`` (serve).

    A content-addressed transcode output is shared by every artefact holding the
    identical source media, so the user may read those identical bytes if ANY
    one entitled artefact is viewable and not restricted for them — that artefact
    is a legitimate, unrestricted route to the same content.  For a legacy
    single-owner path this collapses to the previous per-artefact check.
    """
    from ..visibility import can_view_artefact, output_blocked_for
    viewable = [
        a for a in resolve_output_artefacts(filename)
        if can_view_artefact(a, user, sees_all=sees_all)
    ]
    if not viewable:
        return 'not_found'
    if any(not output_blocked_for(user, a) for a in viewable):
        return 'ok'
    return 'restricted'


def serve_output_file(filename):
    """Serve an analysis output file (visualisation, extracted text, ...).

    S3 mode: redirect to a pre-signed URL.  Local mode: traversal-checked
    send_file with an explicit mimetype (S3-compatible backends and browsers
    both need the Content-Type set — see the storage gotchas in CLAUDE.md).
    Returns None when the file is outside the output folder or missing.
    Callers must have already enforced artefact visibility via
    resolve_output_artefact().
    """
    from arcology_shared.storage import LocalStorage

    storage = current_app.storage
    key = storage.storage_key('outputs', filename)

    url = storage.presigned_url(key)
    if url:
        return redirect(url)

    if isinstance(storage, LocalStorage):
        local_path = str(storage.local_path(key))
        output_dir = str(storage.outputs_dir)
        real_path = os.path.realpath(local_path)
        if not real_path.startswith(os.path.realpath(output_dir) + os.sep):
            return None
        if not os.path.exists(real_path):
            return None
        mime, _ = mimetypes.guess_type(real_path)
        return send_file(real_path, mimetype=mime or 'application/octet-stream')

    return None

# vim: ts=4 sw=4 et
