"""Add ARMLOCK_REMOVE analysis type

Revision ID: 000069c4322f
Revises: 000069c2f0db
Create Date: 2026-03-25

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069c4322f'
down_revision = '000069c45b2d'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
# This flag tells Alembic to run this migration outside a transaction block.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'ARMLOCK_REMOVE'"
        ))


def downgrade():
    # PostgreSQL does not support removing individual enum values without a
    # full type recreation.  The extra enum value is harmless if the code reverts.
    pass

# vim: ts=4 sw=4 et
