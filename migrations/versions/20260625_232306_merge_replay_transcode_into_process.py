"""Merge REPLAY_TRANSCODE into REPLAY_PROCESS

Acorn Replay / ARMovie files were processed by two analyses: REPLAY_PROCESS
(parse the header into replay_movies metadata) and REPLAY_TRANSCODE (transcode
the video to MP4 and attach mp4_output_path/poster_path to the row).  Both
re-discovered and re-parsed the same files; they are now a single REPLAY_PROCESS
job that parses *and* transcodes in one pass.

The REPLAY_TRANSCODE enum member has been removed from the Python AnalysisType.
PostgreSQL cannot drop an enum value, so the 'REPLAY_TRANSCODE' label lingers in
the type harmlessly — but the ORM raises LookupError on a row whose enum holds a
value absent from the Python enum, so any existing REPLAY_TRANSCODE analysis rows
must be removed.  Their transcoded outputs remain attached to the replay_movies
rows (mp4_output_path / *_blob_id are untouched), so already-transcoded movies
keep playing; new extractions transcode via the merged REPLAY_PROCESS handler.

Revision ID: 00006a3db85a
Revises: 00006a3bdbb4
Create Date: 2026-06-25 23:23:06 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a3db85a"
down_revision = "00006a3bdbb4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite (tests) stores the enum as a plain string and never enforces
        # the type, so there is nothing to clean up.
        return
    # Null any FK references first (defensive — REPLAY_TRANSCODE never registers
    # derived artefacts, but mirror the standard enum-removal cleanup), then drop
    # the now-orphaned analysis rows.
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'REPLAY_TRANSCODE')"""))
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type = 'REPLAY_TRANSCODE'"
    ))


def downgrade():
    """No-op: the deleted REPLAY_TRANSCODE analyses cannot be reconstructed.

    The 'REPLAY_TRANSCODE' enum label still exists in the PostgreSQL type (it was
    never dropped), and reverting the code restores the Python member, so there is
    nothing schema-level to undo.  Re-running the analysis pipeline (or
    rebuild-search-index after a re-analysis) regenerates the rows.
    """
    pass

# vim: ts=4 sw=4 et
