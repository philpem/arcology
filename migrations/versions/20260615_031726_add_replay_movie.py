"""Add REPLAY_PROCESS analysis type and replay_movies table

Acorn Replay / ARMovie files (RISC OS filetype &AE7) found inside disc-image
extractions are now parsed by a REPLAY_PROCESS analysis, which records the
movie's header metadata (title, codec, dimensions, sound, duration, …) in the
replay_movies search-index table — mirroring riscos_modules for RISC OS
modules.

Revision ID: 00006a2f6ec6
Revises: 00006a2b0ef2
Create Date: 2026-06-15 03:17:26 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a2f6ec6"
down_revision = "00006a2b0ef2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'REPLAY_PROCESS'"
            ))

    op.create_table(
        'replay_movies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=1000), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('author', sa.String(length=255), nullable=True),
        sa.Column('copyright', sa.String(length=255), nullable=True),
        sa.Column('video_format', sa.Integer(), nullable=True),
        sa.Column('video_label', sa.String(length=64), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('pixel_depth', sa.Integer(), nullable=True),
        sa.Column('frame_rate', sa.Float(), nullable=True),
        sa.Column('sound_format', sa.Integer(), nullable=True),
        sa.Column('sound_rate', sa.Integer(), nullable=True),
        sa.Column('sound_channels', sa.Integer(), nullable=True),
        sa.Column('sound_precision', sa.Integer(), nullable=True),
        sa.Column('frames_per_chunk', sa.Float(), nullable=True),
        sa.Column('number_of_chunks', sa.Integer(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('mp4_output_path', sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_replay_movies_artefact_id'), 'replay_movies', ['artefact_id'], unique=False)
    op.create_index(op.f('ix_replay_movies_file_path'), 'replay_movies', ['file_path'], unique=False)
    op.create_index(op.f('ix_replay_movies_title'), 'replay_movies', ['title'], unique=False)
    op.create_index(op.f('ix_replay_movies_video_format'), 'replay_movies', ['video_format'], unique=False)


def downgrade():
    """Drop the table and clean up REPLAY_PROCESS rows.

    PostgreSQL cannot remove an enum value once added, and the ORM raises
    LookupError on a row holding a value absent from the Python enum, so any
    REPLAY_PROCESS analyses must be deleted on downgrade.
    """
    op.drop_index(op.f('ix_replay_movies_video_format'), table_name='replay_movies')
    op.drop_index(op.f('ix_replay_movies_title'), table_name='replay_movies')
    op.drop_index(op.f('ix_replay_movies_file_path'), table_name='replay_movies')
    op.drop_index(op.f('ix_replay_movies_artefact_id'), table_name='replay_movies')
    op.drop_table('replay_movies')

    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type = 'REPLAY_PROCESS'"
    ))

# vim: ts=4 sw=4 et
