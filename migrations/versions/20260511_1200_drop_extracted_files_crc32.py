"""Drop extracted_files.crc32 column.

The crc32 column on extracted_files was never populated by the worker
(which only computes md5, sha1, sha256) and was never used in hash
matching. Dropping it removes a permanently-NULL column from what can
be a very large table.

KnownFile.crc32 is retained for import/export round-trip compatibility
with external hash databases (e.g. NSRL) that distribute CRC32 values.

Revision ID: 00006a01ece1
Revises: 000069d43024
Create Date: 2026-05-11 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '00006a01ece1'
down_revision = '000069e9644a'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('extracted_files', 'crc32')


def downgrade():
    op.add_column('extracted_files', sa.Column('crc32', sa.String(length=8), nullable=True))

# vim: ts=4 sw=4 et
