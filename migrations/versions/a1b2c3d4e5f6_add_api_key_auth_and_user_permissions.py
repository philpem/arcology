"""Add API key authentication and user permissions

Revision ID: a1b2c3d4e5f6
Revises: 114ecb0fef06
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '114ecb0fef06'
branch_labels = None
depends_on = None


def upgrade():
    # Create enums
    userpermission = sa.Enum('read_only', 'read_write', name='userpermission')
    apikeypermission = sa.Enum('read_only', 'read_upload', 'read_write', name='apikeypermission')
    userpermission.create(op.get_bind())
    apikeypermission.create(op.get_bind())

    # Add new columns to user table
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('permission', sa.Enum('read_only', 'read_write', name='userpermission'), nullable=False, server_default='read_write'))
        batch_op.add_column(sa.Column('can_use_api', sa.Boolean(), nullable=False, server_default='false'))

    # Create api_keys table
    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('key_prefix', sa.String(length=8), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('permission', sa.Enum('read_only', 'read_upload', 'read_write', name='apikeypermission'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_key_hash'), ['key_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_api_keys_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_user_id'))
        batch_op.drop_index(batch_op.f('ix_api_keys_key_hash'))

    op.drop_table('api_keys')

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('can_use_api')
        batch_op.drop_column('permission')
        batch_op.drop_column('is_admin')

    sa.Enum(name='apikeypermission').drop(op.get_bind())
    sa.Enum(name='userpermission').drop(op.get_bind())

# vim: ts=4 sw=4 noet
