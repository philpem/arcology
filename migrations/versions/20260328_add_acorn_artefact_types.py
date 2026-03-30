"""Add ACORN_SPRITE, ACORN_DRAW, ACORN_TEXT to artefacttype enum

Revision ID: 000069caa370
Revises: 000069c4322f
Create Date: 2026-03-28
"""
import sqlalchemy as sa
from alembic import op

revision = '000069caa370'
down_revision = '000069c4322f'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ACORN_SPRITE'"))
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ACORN_DRAW'"))
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ACORN_TEXT'"))


def downgrade():
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass

# vim: ts=4 sw=4 et
