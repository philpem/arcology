"""Add OutputBlob FKs to replay_movies and media_files for transcode dedup.

Content-keyed media transcode dedup stores the transcoded MP4/poster as a
shared, refcounted ``OutputBlob`` (keyed on the source file's hash) rather than
a per-artefact output copy.  These nullable FKs let a ReplayMovie / MediaFile
reference that blob so the storage GC only deletes the bytes once nothing
references them.  Existing rows keep their (artefact-scoped) path strings and
leave the FKs null — they are served via the legacy path and migrate to the
content-addressed scheme on re-analysis.

Revision ID: 00006a3bdbb4
Revises: 00006a3beadf
Create Date: 2026-06-25 01:53:01.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a3bdbb4'
down_revision = '00006a3beadf'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table is dialect-aware: a plain ALTER ADD COLUMN on PostgreSQL,
    # a copy-and-move recreate on SQLite (which cannot ALTER in a FK constraint).
    for table in ('replay_movies', 'media_files'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'mp4_output_blob_id', sa.Integer(),
                sa.ForeignKey('output_blobs.id', ondelete='SET NULL',
                              name=f'fk_{table}_mp4_output_blob'),
                nullable=True))
            batch_op.add_column(sa.Column(
                'poster_blob_id', sa.Integer(),
                sa.ForeignKey('output_blobs.id', ondelete='SET NULL',
                              name=f'fk_{table}_poster_blob'),
                nullable=True))
            batch_op.create_index(
                f'ix_{table}_mp4_output_blob_id', ['mp4_output_blob_id'])
            batch_op.create_index(
                f'ix_{table}_poster_blob_id', ['poster_blob_id'])


def downgrade():
    for table in ('replay_movies', 'media_files'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f'ix_{table}_poster_blob_id')
            batch_op.drop_index(f'ix_{table}_mp4_output_blob_id')
            batch_op.drop_column('poster_blob_id')
            batch_op.drop_column('mp4_output_blob_id')

# vim: ts=4 sw=4 et
