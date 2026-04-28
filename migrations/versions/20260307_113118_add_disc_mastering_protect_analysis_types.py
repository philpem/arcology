"""Add disc_mastering_detect and disc_protection_detect analysis types

Revision ID: 000069ac0c86
Revises: 000069a1137d
Create Date: 2026-03-07 11:31:18.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069ac0c86'
down_revision = '000069a1137d'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
# This flag tells Alembic to run this migration outside a transaction block.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_MASTERING_DETECT'"
        ))
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_PROTECTION_DETECT'"
        ))


def downgrade():
    # PostgreSQL does not support removing individual enum values without a
    # full type recreation, which would require temporarily dropping the
    # analyses table column.  Downgrading is therefore a no-op; the extra
    # enum values are harmless if the code reverts.
    pass

# vim: ts=4 sw=4 et
