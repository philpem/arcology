"""Add product_recognition analysis type

Revision ID: 000069b47d66
Revises: 000069b0e773
Create Date: 2026-03-12 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069b47d66'
down_revision = '000069b0e773'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'product_recognition'"
        ))


def downgrade():
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass

# vim: ts=4 sw=4 et
