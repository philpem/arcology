"""Add TBAFS artefact type

Revision ID: 000069d03ed7
Revises: 000069cbcb4f
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op

revision = '000069d03ed7'
down_revision = '000069cbcb4f'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'TBAFS'"))


def downgrade():
    pass  # PostgreSQL does not support removing enum values

# vim: ts=4 sw=4 et
