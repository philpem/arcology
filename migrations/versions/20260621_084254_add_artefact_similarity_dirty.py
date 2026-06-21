"""Add Artefact.similarity_dirty flag for incremental similarity refresh

Marks an artefact whose extracted-file set has changed so its content-set
similarity cache can be refreshed incrementally (by `flask refresh-similarity`
and the task runner's similarity-delta sweep) rather than only by a full
`flask rebuild-similarity`.

Existing rows default to False: a freshly-migrated cache is already consistent,
so nothing is marked stale.  New extractions set the flag going forward.

Revision ID: 00006a37a40e
Revises: 00006a3523b3
Create Date: 2026-06-21 08:42:54.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a37a40e'
down_revision = '00006a3523b3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'artefacts',
        sa.Column('similarity_dirty', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index(
        'ix_artefacts_similarity_dirty', 'artefacts', ['similarity_dirty'])


def downgrade():
    op.drop_index('ix_artefacts_similarity_dirty', table_name='artefacts')
    op.drop_column('artefacts', 'similarity_dirty')

# vim: ts=4 sw=4 et
