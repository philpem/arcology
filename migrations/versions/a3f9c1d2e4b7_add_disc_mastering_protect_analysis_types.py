"""Add disc_mastering_detect and disc_protection_detect analysis types

Revision ID: a3f9c1d2e4b7
Revises: 114ecb0fef06
Create Date: 2026-03-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3f9c1d2e4b7'
down_revision = '114ecb0fef06'
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL: extend the analysistype enum with the two new values.
    # ALTER TYPE ADD VALUE cannot run inside a transaction, so we open a
    # separate AUTOCOMMIT connection outside Alembic's transaction block.
    # SQLite stores enums as VARCHAR so no DDL change is needed there.
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with bind.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            conn.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_MASTERING_DETECT'"
            ))
            conn.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_PROTECTION_DETECT'"
            ))


def downgrade():
    # PostgreSQL does not support removing individual enum values without a
    # full type recreation, which would require temporarily dropping the
    # analyses table column.  Downgrading is therefore a no-op; the extra
    # enum values are harmless if the code reverts.
    pass
