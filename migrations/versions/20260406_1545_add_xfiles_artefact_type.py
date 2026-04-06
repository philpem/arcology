"""Add XFILES artefact type

Revision ID: 000069d2fd9e
Revises: 000069d058ae
Create Date: 2026-04-06

"""
import sqlalchemy as sa
from alembic import op

revision = '000069d2fd9e'
down_revision = '000069d058ae'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'XFILES'"))


def downgrade():
    pass  # PostgreSQL does not support removing enum values

# vim: ts=4 sw=4 et
