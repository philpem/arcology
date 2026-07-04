"""Drop the write-only md5 column from upload_blobs / output_blobs

Blobs are deduplicated on ``(file_size, sha256)`` — that pair is the content
identity.  The ``md5`` column was carried for compatibility but was only ever
written, never read (the MD5 that callers and the API actually read lives on the
artefact row).  Remove it so the blob is a lean dedup record.

Revision ID: 00006a486a18
Revises: 00006a3e7e7f
Create Date: 2026-07-04

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a486a18'
down_revision = '00006a3e7e7f'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('upload_blobs', 'md5')
    op.drop_column('output_blobs', 'md5')


def downgrade():
    # Re-add as a nullable column; the historical values are unrecoverable (and
    # were never used), so rows come back with md5 = NULL.
    op.add_column('output_blobs', sa.Column('md5', sa.String(length=32), nullable=True))
    op.add_column('upload_blobs', sa.Column('md5', sa.String(length=32), nullable=True))
