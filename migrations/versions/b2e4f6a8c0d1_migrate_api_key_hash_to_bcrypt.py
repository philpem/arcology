"""Migrate API key hash from SHA-256 to bcrypt

Widen key_hash column from String(64) to String(72) to accommodate bcrypt
hashes (60 chars).  Existing keys hashed with SHA-256 are deactivated because
their stored hashes cannot be verified with the new bcrypt scheme.

Revision ID: b2e4f6a8c0d1
Revises: 1c1217874f41
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2e4f6a8c0d1'
down_revision = '1c1217874f41'
branch_labels = None
depends_on = None


def upgrade():
	# Deactivate all existing keys — their SHA-256 hashes cannot be verified
	# with the new bcrypt scheme.  Users must regenerate their API keys.
	op.execute(sa.text("UPDATE api_keys SET is_active = false"))

	# Widen key_hash to hold bcrypt hashes (60 chars; 72 gives headroom).
	with op.batch_alter_table('api_keys', schema=None) as batch_op:
		batch_op.alter_column('key_hash',
			existing_type=sa.String(length=64),
			type_=sa.String(length=72),
			existing_nullable=False)


def downgrade():
	# Re-widening back is safe; old SHA-256 hashes (64 chars) still fit in 72.
	# Keys deactivated during upgrade are not restored.
	with op.batch_alter_table('api_keys', schema=None) as batch_op:
		batch_op.alter_column('key_hash',
			existing_type=sa.String(length=72),
			type_=sa.String(length=64),
			existing_nullable=False)

# vim: ts=4 sw=4 noet
