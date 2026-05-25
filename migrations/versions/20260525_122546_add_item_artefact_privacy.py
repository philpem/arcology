"""Add ownership and privacy columns to items and artefacts

Adds:
  * items.owner_id, items.is_private, items.private_effective
  * artefacts.owner_id, artefacts.is_private

Privacy is set explicitly on an item or artefact (is_private) and descends
strictly to all sub-items/artefacts.  items.private_effective is the
denormalised "own flag OR any ancestor private" used for cheap query filtering;
it is backfilled to equal is_private (all existing data starts public, so all
flags are False).

Revision ID: 00006a143fca
Revises: 00006a080919
Create Date: 2026-05-25
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a143fca'
down_revision = '00006a080919'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('items', sa.Column('owner_id', sa.Integer(), nullable=True))
    op.add_column('items', sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('items', sa.Column('private_effective', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('ix_items_owner_id', 'items', ['owner_id'])
    op.create_index('ix_items_private_effective', 'items', ['private_effective'])
    op.create_foreign_key('fk_items_owner_id_user', 'items', 'user', ['owner_id'], ['id'], ondelete='SET NULL')

    op.add_column('artefacts', sa.Column('owner_id', sa.Integer(), nullable=True))
    op.add_column('artefacts', sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('ix_artefacts_owner_id', 'artefacts', ['owner_id'])
    op.create_foreign_key('fk_artefacts_owner_id_user', 'artefacts', 'user', ['owner_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint('fk_artefacts_owner_id_user', 'artefacts', type_='foreignkey')
    op.drop_index('ix_artefacts_owner_id', table_name='artefacts')
    op.drop_column('artefacts', 'is_private')
    op.drop_column('artefacts', 'owner_id')

    op.drop_constraint('fk_items_owner_id_user', 'items', type_='foreignkey')
    op.drop_index('ix_items_private_effective', table_name='items')
    op.drop_index('ix_items_owner_id', table_name='items')
    op.drop_column('items', 'private_effective')
    op.drop_column('items', 'is_private')
    op.drop_column('items', 'owner_id')

# vim: ts=4 sw=4 et
