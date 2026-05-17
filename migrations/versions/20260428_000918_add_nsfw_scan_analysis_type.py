"""Add NSFW_SCAN to analysistype enum.

Revision ID: 000069effaae
Revises: 000069e9644a
Create Date: 2026-04-28 00:09:18.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069effaae'
down_revision = '000069e9644a'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'NSFW_SCAN'"))


def downgrade():
    pass  # PostgreSQL does not support removing enum values
