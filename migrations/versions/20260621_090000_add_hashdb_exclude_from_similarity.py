"""Add HashDatabase.exclude_from_similarity flag

When set, extracted files linked to this hash database are dropped from the
content-set similarity computation, so base-OS / runtime boilerplate (e.g. a
stock RISC OS install) does not make every system disc match every other.
Reserve it for the operating system, not application software.

Revision ID: 00006a3670f7
Revises: 00006a37a40e
Create Date: 2026-06-20 10:52:39.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a3670f7'
down_revision = '00006a37a40e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'hash_databases',
        sa.Column('exclude_from_similarity', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column('hash_databases', 'exclude_from_similarity')

# vim: ts=4 sw=4 et
