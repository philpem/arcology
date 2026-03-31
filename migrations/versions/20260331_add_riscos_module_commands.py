"""Add commands column to riscos_modules table

Revision ID: 000069cbb243
Revises: 000069cb3001
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = '000069cbb243'
down_revision = '000069cb3001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('riscos_modules', sa.Column('commands', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('riscos_modules', 'commands')

# vim: ts=4 sw=4 et
