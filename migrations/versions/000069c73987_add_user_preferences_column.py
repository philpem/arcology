"""Add JSON preferences column to user table

Revision ID: 000069c73987
Revises: 000069c47146
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa

revision = '000069c73987'
down_revision = '000069c47146'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('preferences', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('user', 'preferences')
