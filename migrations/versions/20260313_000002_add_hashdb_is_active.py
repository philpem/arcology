"""Add is_active to hash_databases

Allows individual hash databases to be disabled without deleting them.
Disabled databases are excluded from hash lookups (find_known_file) and
from rescan operations.

Revision ID: 000069b45216
Revises: 000069b41ab4
Create Date: 2026-03-13 00:00:02.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069b45216'
down_revision = '000069b41ab4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'hash_databases',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
    )


def downgrade():
    op.drop_column('hash_databases', 'is_active')

# vim: ts=4 sw=4 et
