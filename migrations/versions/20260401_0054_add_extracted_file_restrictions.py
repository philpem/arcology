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

_RESTRICTION_VALUES = ('MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED')


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # On PostgreSQL, reference the existing restrictiontype enum created by the
    # add_download_restrictions migration.  We must use postgresql.ENUM with
    # create_type=False here — sa.Enum ignores that flag and always tries to
    # CREATE TYPE, which fails with "type already exists".
    if is_pg:
        from sqlalchemy.dialects import postgresql
        col_type = postgresql.ENUM(*_RESTRICTION_VALUES, name='restrictiontype',
                                   create_type=False)
    else:
        col_type = sa.String(50)

    op.create_table(
        'extracted_file_restrictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('extracted_file_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type', col_type, nullable=False),
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
    # The restrictiontype enum is shared with artefact_restrictions and must
    # not be dropped here — it will be removed by the add_download_restrictions
    # downgrade if that migration is also rolled back.

# vim: ts=4 sw=4 et
