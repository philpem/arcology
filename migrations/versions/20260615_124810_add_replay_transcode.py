"""Add REPLAY_TRANSCODE analysis type and replay_movies.poster_path

Acorn Replay / ARMovie files can now be transcoded to MP4 by a REPLAY_TRANSCODE
analysis (scotch's replay-transcode + ffmpeg).  The resulting MP4 and a
first-frame poster thumbnail are recorded on the existing replay_movies row
(mp4_output_path already exists; poster_path is added here).

Revision ID: 00006a2ff48a
Revises: 00006a2f6ec6
Create Date: 2026-06-15 12:48:10 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a2ff48a"
down_revision = "00006a2f6ec6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'REPLAY_TRANSCODE'"
            ))

    op.add_column(
        'replay_movies',
        sa.Column('poster_path', sa.String(length=1000), nullable=True),
    )


def downgrade():
    """Drop poster_path and clean up REPLAY_TRANSCODE rows.

    PostgreSQL cannot remove an enum value once added, and the ORM raises
    LookupError on a row holding a value absent from the Python enum, so any
    REPLAY_TRANSCODE analyses must be deleted on downgrade.
    """
    op.drop_column('replay_movies', 'poster_path')

    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type = 'REPLAY_TRANSCODE'"
    ))

# vim: ts=4 sw=4 et
