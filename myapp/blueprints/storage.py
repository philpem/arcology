"""
Arcology - Storage Blueprint

Staff/admin view of storage capacity and deduplication statistics.
"""

from flask import Blueprint, abort, render_template, request
from flask_login import login_required
from ..permissions import require_permission
from ..services.storage_stats import (
    deduplication_stats,
    duplicate_group_instances,
    format_size,
    storage_capacity,
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/storage', template_folder='../templates')


def init_app(app):
    """No main-menu item: the navbar link is rendered conditionally for staff
    in _base.html (the main menu shows to every user)."""
    pass


@blueprint.route('/')
@login_required
@require_permission('staff')
def index():
    """Disk usage and deduplication statistics (STAFF and admins only)."""
    dedup = deduplication_stats()
    return render_template(
        'storage/index.html',
        # Reuse the footprint already summed by deduplication_stats() instead of
        # re-scanning the blob tables inside storage_capacity().
        capacity=storage_capacity(arcology_bytes=dedup['physical_bytes']),
        dedup=dedup,
        format_size=format_size,
    )


@blueprint.route('/duplicates')
@login_required
@require_permission('staff')
def duplicates():
    """Drill into one duplicated content group: every artefact and extracted
    file holding copies of ``(size, sha256)`` (STAFF and admins only)."""
    file_size = request.args.get('size', type=int)
    sha256 = (request.args.get('sha256') or '').strip().lower()
    # Zero-length content is excluded from the dedup reports (it wastes no
    # bytes), so there is nothing meaningful to drill into for size 0.
    if not sha256 or file_size is None or file_size <= 0:
        abort(404)
    group = duplicate_group_instances(file_size, sha256)
    if not group['artefacts'] and not group['files']:
        abort(404)
    return render_template(
        'storage/duplicates.html',
        group=group,
        format_size=format_size,
    )


# vim: ts=4 sw=4 et
