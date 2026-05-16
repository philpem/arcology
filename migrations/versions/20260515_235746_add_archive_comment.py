"""Add archive_comment columns to partitions and extracted_files

Revision ID: 000069fabf0d
Revises: 00006a01ece1
Create Date: 2026-05-15
"""
import sqlalchemy as sa
from alembic import op

revision = '000069fabf0d'
down_revision = '00006a01ece1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('partitions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archive_comment', sa.Text(), nullable=True))
    with op.batch_alter_table('extracted_files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archive_comment', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('extracted_files', schema=None) as batch_op:
        batch_op.drop_column('archive_comment')
    with op.batch_alter_table('partitions', schema=None) as batch_op:
        batch_op.drop_column('archive_comment')

# vim: ts=4 sw=4 et
