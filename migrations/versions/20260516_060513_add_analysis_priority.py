"""Add priority column to analyses table

Revision ID: 00006a080919
Revises: 000069fabf0d
Create Date: 2026-05-16
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a080919'
down_revision = '000069fabf0d'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analyses', sa.Column('priority', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_analyses_priority', 'analyses', ['priority'])


def downgrade():
    op.drop_index('ix_analyses_priority', table_name='analyses')
    op.drop_column('analyses', 'priority')

# vim: ts=4 sw=4 et
