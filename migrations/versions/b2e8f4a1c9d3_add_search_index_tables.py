"""Add search index tables for protection/mastering indicators and partition gnu_file_type

Revision ID: b2e8f4a1c9d3
Revises: 1c1217874f41
Create Date: 2026-03-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2e8f4a1c9d3'
down_revision = '1c1217874f41'
branch_labels = None
depends_on = None


def upgrade():
    # New table: artefact_protection
    # Stores copy protection indicator rows extracted from DISC_PROTECTION_DETECT
    # analysis results.  Populated server-side when an analysis completes.
    op.create_table(
        'artefact_protection',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('protection_type', sa.String(length=64), nullable=False),
        sa.Column('track', sa.Integer(), nullable=True),
        sa.Column('side', sa.Integer(), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artefact_protection_artefact_id', 'artefact_protection', ['artefact_id'])
    op.create_index('ix_artefact_protection_protection_type', 'artefact_protection', ['protection_type'])

    # New table: artefact_mastering
    # Stores mastering/duplicator fingerprint indicator rows extracted from
    # DISC_MASTERING_DETECT analysis results.
    op.create_table(
        'artefact_mastering',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('mastering_type', sa.String(length=64), nullable=False),
        sa.Column('track', sa.Integer(), nullable=True),
        sa.Column('decoded', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artefact_mastering_artefact_id', 'artefact_mastering', ['artefact_id'])
    op.create_index('ix_artefact_mastering_mastering_type', 'artefact_mastering', ['mastering_type'])

    # New column on partition: gnu_file_type
    # Stores the output of file(1) as reported by PARTITION_DETECT.
    op.add_column('partitions', sa.Column('gnu_file_type', sa.String(length=256), nullable=True))
    op.create_index('ix_partitions_gnu_file_type', 'partitions', ['gnu_file_type'])


def downgrade():
    op.drop_index('ix_partitions_gnu_file_type', table_name='partitions')
    op.drop_column('partitions', 'gnu_file_type')

    op.drop_index('ix_artefact_mastering_mastering_type', table_name='artefact_mastering')
    op.drop_index('ix_artefact_mastering_artefact_id', table_name='artefact_mastering')
    op.drop_table('artefact_mastering')

    op.drop_index('ix_artefact_protection_protection_type', table_name='artefact_protection')
    op.drop_index('ix_artefact_protection_artefact_id', table_name='artefact_protection')
    op.drop_table('artefact_protection')
