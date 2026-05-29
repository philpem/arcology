"""Add item sharing ACL: groups, group_memberships, item_shares

Revision ID: 00006a17b70e
Revises: 00006a144022
Create Date: 2026-05-28

Introduces three-tier per-item share permissions:
  viewer  — read-only access
  editor  — can add/modify content (artefacts, child items, references)
  curator — full co-curation rights: privacy toggle, share management
            (ownership transfer always requires the actual owner or an admin)
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a17b70e'
down_revision = '00006a144022'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, server_default='local'),
        sa.Column('oidc_claim_name', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_groups_name'),
    )
    op.create_index('ix_groups_name', 'groups', ['name'])
    op.create_index('ix_groups_oidc_claim_name', 'groups', ['oidc_claim_name'])

    op.create_table(
        'group_memberships',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'group_id'),
    )

    op.create_table(
        'item_shares',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('permission', sa.String(20), nullable=False, server_default='viewer'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_id', 'user_id', name='uq_item_share_user'),
        sa.UniqueConstraint('item_id', 'group_id', name='uq_item_share_group'),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)",
            name='ck_item_shares_exactly_one_principal',
        ),
    )
    op.create_index('ix_item_shares_item_id', 'item_shares', ['item_id'])
    op.create_index('ix_item_shares_user_id', 'item_shares', ['user_id'])
    op.create_index('ix_item_shares_group_id', 'item_shares', ['group_id'])


def downgrade():
    op.drop_index('ix_item_shares_group_id', table_name='item_shares')
    op.drop_index('ix_item_shares_user_id', table_name='item_shares')
    op.drop_index('ix_item_shares_item_id', table_name='item_shares')
    op.drop_table('item_shares')
    op.drop_table('group_memberships')
    op.drop_index('ix_groups_oidc_claim_name', table_name='groups')
    op.drop_index('ix_groups_name', table_name='groups')
    op.drop_table('groups')

# vim: ts=4 sw=4 et
