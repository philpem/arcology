"""Fix PRODUCT_RECOGNITION enum value case in analysistype

The previous migration (e2f3a4b5c6d7) added 'product_recognition' (lowercase)
to the analysistype PostgreSQL enum, but SQLAlchemy stores enum names in
uppercase (e.g. 'FILE_EXTRACTION', 'ARCHIVE_DETECT'). This migration adds the
correct uppercase value 'PRODUCT_RECOGNITION'.

Revision ID: a1b2c3d4e5f6
Revises: f3a4b5c6d7e8
Create Date: 2026-03-13 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'PRODUCT_RECOGNITION'"
        ))


def downgrade():
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass

# vim: ts=4 sw=4 et
