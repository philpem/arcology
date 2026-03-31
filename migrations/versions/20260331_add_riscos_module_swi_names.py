"""Add swi_names column to riscos_modules

Revision ID: 000069cbb651
Revises: 000069cbb243
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = '000069cbb651'
down_revision = '000069cbb243'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('riscos_modules', sa.Column('swi_names', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('riscos_modules', 'swi_names')

# vim: ts=4 sw=4 et
