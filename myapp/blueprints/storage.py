"""
Arcology - Storage Blueprint

Staff/admin view of storage capacity and deduplication statistics.
"""

from flask import Blueprint, render_template
from flask_login import login_required
from ..permissions import require_permission
from ..services.storage_stats import deduplication_stats, format_size, storage_capacity

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


# vim: ts=4 sw=4 et
