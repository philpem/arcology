"""Add MEDIA_TRANSCODE analysis, VIDEO/AUDIO artefact types, media_files table

Generic time-based media (audio/video) found inside extractions — or uploaded
directly — is now playable in the viewer.  Browser-native containers (MP4/WebM/
MP3/...) are streamed directly; non-native ones (AVI/QuickTime/MPEG/...) are
transcoded to a browser-playable MP4/M4A by a MEDIA_TRANSCODE analysis, which
records the transcoded output plus ffprobe codec/track metadata in the
media_files search-index table — mirroring replay_movies for Acorn Replay.

Revision ID: 00006a3b25dd
Revises: 00006a39cf93
Create Date: 2026-06-24 00:33:33 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a3b25dd"
down_revision = "00006a39cf93"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'MEDIA_TRANSCODE'"
            ))
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'VIDEO'"
            ))
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'AUDIO'"
            ))

    op.create_table(
        'media_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=1000), nullable=True),
        sa.Column('media_kind', sa.String(length=8), nullable=True),
        sa.Column('container_format', sa.String(length=64), nullable=True),
        sa.Column('video_codec', sa.String(length=64), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('frame_rate', sa.Float(), nullable=True),
        sa.Column('audio_codec', sa.String(length=64), nullable=True),
        sa.Column('sample_rate', sa.Integer(), nullable=True),
        sa.Column('channels', sa.Integer(), nullable=True),
        sa.Column('has_audio', sa.Boolean(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('mp4_output_path', sa.String(length=1000), nullable=True),
        sa.Column('poster_path', sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_media_files_artefact_id'), 'media_files', ['artefact_id'], unique=False)
    op.create_index(op.f('ix_media_files_file_path'), 'media_files', ['file_path'], unique=False)
    op.create_index(op.f('ix_media_files_media_kind'), 'media_files', ['media_kind'], unique=False)


def downgrade():
    """Drop the table and clean up MEDIA_TRANSCODE / VIDEO / AUDIO rows.

    PostgreSQL cannot remove an enum value once added, and the ORM raises
    LookupError on a row holding a value absent from the Python enum, so the
    MEDIA_TRANSCODE analyses are deleted and any VIDEO/AUDIO artefacts are
    remapped to UNKNOWN (preserving the files; they re-route through
    FORMAT_IDENTIFY on re-analysis).
    """
    op.drop_index(op.f('ix_media_files_media_kind'), table_name='media_files')
    op.drop_index(op.f('ix_media_files_file_path'), table_name='media_files')
    op.drop_index(op.f('ix_media_files_artefact_id'), table_name='media_files')
    op.drop_table('media_files')

    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type = 'MEDIA_TRANSCODE'"
    ))
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type IN ('VIDEO', 'AUDIO')"
    ))

# vim: ts=4 sw=4 et
