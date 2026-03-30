"""Add FORMAT_CONVERT to analysistype enum

Revision ID: 000069caa371
Revises: 000069caa370
Create Date: 2026-03-28
"""
import sqlalchemy as sa
from alembic import op

revision = '000069caa371'
down_revision = '000069caa370'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'FORMAT_CONVERT'"))


def downgrade():
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass

# vim: ts=4 sw=4 et
