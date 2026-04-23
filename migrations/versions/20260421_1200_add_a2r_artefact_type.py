"""Add A2R artefact type

Revision ID: 000069e74e9c
Revises: 000069e66a3f
Create Date: 2026-04-21
"""
import sqlalchemy as sa
from alembic import op

revision = '000069e74e9c'
down_revision = '000069e72f63'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'A2R'"))


def downgrade():
    pass  # PostgreSQL does not support removing enum values

# vim: ts=4 sw=4 et
