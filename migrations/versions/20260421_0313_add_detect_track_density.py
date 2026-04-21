"""Add DETECT_TRACK_DENSITY to analysistype enum

Revision ID: 000069e6eb77
Revises: 000069e72f63
Create Date: 2026-04-21
"""
import sqlalchemy as sa
from alembic import op

revision = '000069e6eb77'
down_revision = '000069e72f63'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
# env.py uses transaction_per_migration=True, so we opt out here.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DETECT_TRACK_DENSITY'"
        ))


def downgrade():
    pass  # PostgreSQL cannot remove enum values

# vim: ts=4 sw=4 et
