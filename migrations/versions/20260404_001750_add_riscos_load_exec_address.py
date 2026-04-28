"""Add RISC OS load/exec address columns to extracted_files

Revision ID: 000069d058ae
Revises: 000069d03ed7
Create Date: 2026-04-04

"""
import sqlalchemy as sa
from alembic import op

revision = '000069d058ae'
down_revision = '000069d03ed7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('extracted_files', sa.Column('load_address', sa.String(8), nullable=True))
    op.add_column('extracted_files', sa.Column('exec_address', sa.String(8), nullable=True))


def downgrade():
    op.drop_column('extracted_files', 'exec_address')
    op.drop_column('extracted_files', 'load_address')
