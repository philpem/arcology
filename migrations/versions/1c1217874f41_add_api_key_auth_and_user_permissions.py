"""Add API key authentication and user permissions

Revision ID: 1c1217874f41
Revises: a3f9c1d2e4b7
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM


# revision identifiers, used by Alembic.
revision = '1c1217874f41'
down_revision = 'a3f9c1d2e4b7'
branch_labels = None
depends_on = None

# Define PostgreSQL enum types once, with create_type=False so that
# SQLAlchemy never implicitly issues CREATE TYPE behind our back.
userpermission = PG_ENUM('read_only', 'read_write', name='userpermission', create_type=False)
apikeypermission = PG_ENUM('read_only', 'read_upload', 'read_write', name='apikeypermission', create_type=False)


def upgrade():
	# Explicitly create the enum types (checkfirst=True for idempotency
	# in case a previous failed run already created them).
	bind = op.get_bind()
	if bind.dialect.name == 'postgresql':
		PG_ENUM('read_only', 'read_write', name='userpermission').create(bind, checkfirst=True)
		PG_ENUM('read_only', 'read_upload', 'read_write', name='apikeypermission').create(bind, checkfirst=True)

	# Add new columns to user table
	with op.batch_alter_table('user', schema=None) as batch_op:
		batch_op.add_column(sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='false'))
		batch_op.add_column(sa.Column('permission', userpermission, nullable=False, server_default='read_write'))
		batch_op.add_column(sa.Column('can_use_api', sa.Boolean(), nullable=False, server_default='false'))

	# Create api_keys table
	op.create_table('api_keys',
	sa.Column('id', sa.Integer(), nullable=False),
	sa.Column('user_id', sa.Integer(), nullable=False),
	sa.Column('name', sa.String(length=100), nullable=False),
	sa.Column('key_prefix', sa.String(length=8), nullable=False),
	sa.Column('key_hash', sa.String(length=64), nullable=False),
	sa.Column('permission', apikeypermission, nullable=False),
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

	bind = op.get_bind()
	if bind.dialect.name == 'postgresql':
		PG_ENUM(name='apikeypermission').drop(bind)
		PG_ENUM(name='userpermission').drop(bind)

# vim: ts=4 sw=4 noet
