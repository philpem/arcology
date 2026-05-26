"""Add OIDC/SSO fields to the user table

Revision ID: 00006a144022
Revises: 00006a143fca
Create Date: 2026-05-25
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a144022'
down_revision = '00006a143fca'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('oidc_sub', sa.String(255), nullable=True))
    op.add_column('user', sa.Column('email', sa.String(255), nullable=True))
    op.add_column('user', sa.Column('oidc_managed', sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.create_unique_constraint('uq_user_oidc_sub', 'user', ['oidc_sub'])
    op.create_index('ix_user_oidc_sub', 'user', ['oidc_sub'])


def downgrade():
    op.drop_index('ix_user_oidc_sub', table_name='user')
    op.drop_constraint('uq_user_oidc_sub', 'user', type_='unique')
    op.drop_column('user', 'oidc_managed')
    op.drop_column('user', 'email')
    op.drop_column('user', 'oidc_sub')

# vim: ts=4 sw=4 et
