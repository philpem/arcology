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
from ..database import Artefact
from .artefact_storage import (
    get_artefact_path,
    get_artefact_storage_key,
    resolve_extracted_file_path,
)

# Length of a UUID hex string as used in output paths.
UUID_HEX_LEN = 32


def serve_artefact_file(artefact):
    """Serve an artefact's stored file as an attachment download.

    S3 mode: redirect to a pre-signed URL.  Local mode: send_file (None when
    the file is missing on disk).  Callers must have already enforced
    visibility and download restrictions.
    """
    storage = current_app.storage
    key = get_artefact_storage_key(artefact)

    url = storage.presigned_url(key, filename=artefact.original_filename)
    if url:
        return redirect(url)

    full_path = get_artefact_path(artefact)
    if not os.path.exists(full_path):
        return None
    return send_file(
        full_path,
        as_attachment=True,
        download_name=artefact.original_filename,
    )


def serve_extracted_file(ef):
    """Serve an extracted file as an attachment download.

    Resolves the file's on-disk location via the extraction outputs (handles
    S3 by downloading to a temp file).  Returns None when the file cannot be
    found.  Callers must have already enforced visibility and restrictions.
    """
    file_path = resolve_extracted_file_path(ef)
    if not file_path:
        return None
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
