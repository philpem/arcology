"""Add artefact_distinctiveness cache table

Stores the per-artefact "how unusual is this disc" metric (the inverse lens of
similarity): unique-file counts/bytes, an IDF-weighted distinctiveness score, and
a small JSON list of the rarest files for display. Recomputed wholesale by
`flask rebuild-similarity`.

Revision ID: 00006a389a33
Revises: 00006a3670f7
Create Date: 2026-06-22 02:13:07.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a389a33'
down_revision = '00006a3670f7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'artefact_distinctiveness',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('total_files', sa.Integer(), nullable=False),
        sa.Column('total_bytes', sa.BigInteger(), nullable=False),
        sa.Column('unique_files', sa.Integer(), nullable=False),
        sa.Column('unique_bytes', sa.BigInteger(), nullable=False),
        sa.Column('distinctiveness', sa.Float(), nullable=False),
        sa.Column('top_files', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artefact_distinctiveness_artefact_id',
                    'artefact_distinctiveness', ['artefact_id'], unique=True)


def downgrade():
    op.drop_index('ix_artefact_distinctiveness_artefact_id',
                  table_name='artefact_distinctiveness')
    op.drop_table('artefact_distinctiveness')

# vim: ts=4 sw=4 et
