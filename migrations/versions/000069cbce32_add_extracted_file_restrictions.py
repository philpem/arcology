"""Add extracted_file_restrictions table

Revision ID: 000069cbce32
Revises: 000069cbb651
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = '000069cbce32'
down_revision = '000069cbb651'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'extracted_file_restrictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('extracted_file_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type',
                  sa.Enum('MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED',
                          name='restrictiontype', create_type=False),
                  nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('added_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['added_by_id'], ['user.id']),
        sa.ForeignKeyConstraint(['extracted_file_id'], ['extracted_files.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('extracted_file_id', 'restriction_type',
                            name='uq_extracted_file_restriction_type'),
    )
    op.create_index(
        'ix_extracted_file_restrictions_extracted_file_id',
        'extracted_file_restrictions',
        ['extracted_file_id'],
    )


def downgrade():
    op.drop_index(
        'ix_extracted_file_restrictions_extracted_file_id',
        table_name='extracted_file_restrictions',
    )
    op.drop_table('extracted_file_restrictions')

# vim: ts=4 sw=4 et
