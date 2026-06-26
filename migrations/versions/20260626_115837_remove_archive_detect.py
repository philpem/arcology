"""Remove the ARCHIVE_DETECT analysis type

Archive *detection* is no longer a separate analysis: it is folded into file
registration (``detect_and_queue_archives`` in the worker), which marks archives
and queues ARCHIVE_EXTRACT inline using the file ids the registration API now
returns.  The ARCHIVE_DETECT enum member has been removed from the Python
AnalysisType.

PostgreSQL cannot drop an enum value, so the 'ARCHIVE_DETECT' label lingers in
the type harmlessly — but the ORM raises LookupError on a row whose enum holds a
value absent from the Python enum, so any existing ARCHIVE_DETECT analysis rows
must be removed.  They are pure scan jobs that registered no derived artefacts,
so deleting them loses nothing (re-running an extraction re-detects archives).

Revision ID: 00006a3e696d
Revises: 00006a3db85a
Create Date: 2026-06-26 11:58:37 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a3e696d"
down_revision = "00006a3db85a"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite (tests) stores the enum as a plain string and never enforces
        # the type, so there is nothing to clean up.
        return
    # Null any FK references first (defensive — ARCHIVE_DETECT never registers
    # derived artefacts, but mirror the standard enum-removal cleanup), then drop
    # the now-orphaned analysis rows.
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'ARCHIVE_DETECT')"""))
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type = 'ARCHIVE_DETECT'"
    ))


def downgrade():
    """No-op: the deleted ARCHIVE_DETECT scan jobs cannot be reconstructed.

    The 'ARCHIVE_DETECT' enum label still exists in the PostgreSQL type (it was
    never dropped), and reverting the code restores the Python member, so there
    is nothing schema-level to undo.  Re-running an extraction regenerates the
    per-archive ARCHIVE_EXTRACT jobs.
    """
    pass

# vim: ts=4 sw=4 et
