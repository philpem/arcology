"""Add artefact similarity cache tables and TLSH fuzzy-hash columns

Backs the content-set similarity feature:

* ``artefact_similarity``  -- cached size-weighted Jaccard between two artefacts
* ``artefact_components``   -- directory-subtree components of an artefact
* ``component_similarity``  -- cached similarity between two components
* ``artefacts.tlsh`` / ``extracted_files.tlsh`` -- optional TLSH digest for
  byte-level near-duplicate detection (NULL when not computed, too small, a flux
  type, or py-tlsh is absent).

The three tables are a derived cache rebuilt by ``flask rebuild-similarity``; no
source data lives in them, so the downgrade simply drops everything added here.

Revision ID: 00006a2f7086
Revises: 00006a365847
Create Date: 2026-06-19 10:30:01.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a2f7086'
down_revision = '00006a365847'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'artefact_similarity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_a_id', sa.Integer(), nullable=False),
        sa.Column('artefact_b_id', sa.Integer(), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('shared_files', sa.Integer(), nullable=False),
        sa.Column('union_files', sa.Integer(), nullable=False),
        sa.Column('shared_bytes', sa.BigInteger(), nullable=False),
        sa.Column('union_bytes', sa.BigInteger(), nullable=False),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['artefact_a_id'], ['artefacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['artefact_b_id'], ['artefacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('artefact_a_id', 'artefact_b_id', name='uq_artefact_similarity_pair'),
    )
    op.create_index('ix_artefact_similarity_a', 'artefact_similarity', ['artefact_a_id', 'score'])
    op.create_index('ix_artefact_similarity_b', 'artefact_similarity', ['artefact_b_id', 'score'])
    op.create_index(op.f('ix_artefact_similarity_artefact_a_id'), 'artefact_similarity', ['artefact_a_id'])
    op.create_index(op.f('ix_artefact_similarity_artefact_b_id'), 'artefact_similarity', ['artefact_b_id'])

    op.create_table(
        'artefact_components',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('partition_id', sa.Integer(), nullable=False),
        sa.Column('root_path', sa.String(length=1000), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('file_count', sa.Integer(), nullable=False),
        sa.Column('total_bytes', sa.BigInteger(), nullable=False),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['partition_id'], ['partitions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artefact_components_artefact', 'artefact_components', ['artefact_id'])
    op.create_index(op.f('ix_artefact_components_artefact_id'), 'artefact_components', ['artefact_id'])
    op.create_index(op.f('ix_artefact_components_partition_id'), 'artefact_components', ['partition_id'])
    op.create_index(op.f('ix_artefact_components_uuid'), 'artefact_components', ['uuid'], unique=True)

    op.create_table(
        'component_similarity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('component_a_id', sa.Integer(), nullable=False),
        sa.Column('component_b_id', sa.Integer(), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('shared_files', sa.Integer(), nullable=False),
        sa.Column('union_files', sa.Integer(), nullable=False),
        sa.Column('shared_bytes', sa.BigInteger(), nullable=False),
        sa.Column('union_bytes', sa.BigInteger(), nullable=False),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['component_a_id'], ['artefact_components.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['component_b_id'], ['artefact_components.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('component_a_id', 'component_b_id', name='uq_component_similarity_pair'),
    )
    op.create_index('ix_component_similarity_a', 'component_similarity', ['component_a_id', 'score'])
    op.create_index('ix_component_similarity_b', 'component_similarity', ['component_b_id', 'score'])
    op.create_index(op.f('ix_component_similarity_component_a_id'), 'component_similarity', ['component_a_id'])
    op.create_index(op.f('ix_component_similarity_component_b_id'), 'component_similarity', ['component_b_id'])

    op.add_column('artefacts', sa.Column('tlsh', sa.String(length=72), nullable=True))
    op.add_column('extracted_files', sa.Column('tlsh', sa.String(length=72), nullable=True))


def downgrade():
    op.drop_column('extracted_files', 'tlsh')
    op.drop_column('artefacts', 'tlsh')
    op.drop_table('component_similarity')
    op.drop_table('artefact_components')
    op.drop_table('artefact_similarity')

# vim: ts=4 sw=4 et
