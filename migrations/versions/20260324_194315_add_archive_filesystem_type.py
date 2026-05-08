"""Add ARCHIVE to FilesystemType enum

Revision ID: 000069c2e953
Revises: 000069b4c8ec
Create Date: 2026-03-24
"""
import sqlalchemy as sa
from alembic import op

revision = '000069c2e953'
down_revision = '000069b4c8ec'
branch_labels = None
depends_on = None

# Non-transactional DDL required for ALTER TYPE ... ADD VALUE
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE filesystemtype ADD VALUE IF NOT EXISTS 'ARCHIVE'"))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # Remap partitions using ARCHIVE back to UNKNOWN so the ORM doesn't raise
    # LookupError after this migration is rolled back.  Deleting the rows is
    # not appropriate here; the data is still valid, just re-classified.
    op.execute(sa.text(
        "UPDATE partitions SET filesystem = 'UNKNOWN' WHERE filesystem = 'ARCHIVE'"
    ))

# vim: ts=4 sw=4 et
