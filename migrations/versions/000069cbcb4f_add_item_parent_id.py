"""Add parent_id to items for hierarchical collections

Revision ID: 000069cbcb4f
Revises: 000069cbb651
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = '000069cbcb4f'
down_revision = '000069cbb651'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('items', sa.Column('parent_id', sa.Integer(), nullable=True))
    op.create_index('ix_items_parent_id', 'items', ['parent_id'])
    op.create_foreign_key('fk_items_parent_id', 'items', 'items', ['parent_id'], ['id'])


def downgrade():
    op.drop_constraint('fk_items_parent_id', 'items', type_='foreignkey')
    op.drop_index('ix_items_parent_id', table_name='items')
    op.drop_column('items', 'parent_id')
