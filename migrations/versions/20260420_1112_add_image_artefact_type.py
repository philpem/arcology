"""Add IMAGE artefact type

Revision ID: 000069e60a13
Revises: 000069e74e9c
Create Date: 2026-04-20

"""
import sqlalchemy as sa
from alembic import op

revision = '000069e60a13'
down_revision = '000069e74e9c'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'IMAGE'"))


def downgrade():
    pass  # PostgreSQL does not support removing enum values

# vim: ts=4 sw=4 et
