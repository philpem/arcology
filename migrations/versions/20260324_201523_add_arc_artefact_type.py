"""Add ARC to ArtefactType enum

Revision ID: 000069c2f0db
Revises: 000069c2e953
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

revision = '000069c2f0db'
down_revision = '000069c2e953'
branch_labels = None
depends_on = None

# Non-transactional DDL required for ALTER TYPE ... ADD VALUE
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ARC'"))


def downgrade():
    # PostgreSQL does not support removing enum values; leave it in place.
    pass

# vim: ts=4 sw=4 et
