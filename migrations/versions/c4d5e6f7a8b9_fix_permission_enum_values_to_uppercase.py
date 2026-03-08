"""Fix permission enum values to uppercase

Previous migration created userpermission and apikeypermission enum types
with lowercase values (read_only, read_write, read_upload), but SQLAlchemy
stores Python enum member names which are uppercase (READ_ONLY, READ_WRITE,
READ_UPLOAD). Rename the enum values in-place.

Revision ID: c4d5e6f7a8b9
Revises: b2e4f6a8c0d1
Create Date: 2026-03-08 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b2e4f6a8c0d1'
branch_labels = None
depends_on = None


def upgrade():
	bind = op.get_bind()
	if bind.dialect.name == 'postgresql':
		# Rename userpermission values from lowercase to uppercase.
		# Use a DO block to silently skip values that are already uppercase.
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE userpermission RENAME VALUE 'read_only' TO 'READ_ONLY';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE userpermission RENAME VALUE 'read_write' TO 'READ_WRITE';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))

		# Rename apikeypermission values from lowercase to uppercase.
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'read_only' TO 'READ_ONLY';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'read_upload' TO 'READ_UPLOAD';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'read_write' TO 'READ_WRITE';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))


def downgrade():
	bind = op.get_bind()
	if bind.dialect.name == 'postgresql':
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE userpermission RENAME VALUE 'READ_ONLY' TO 'read_only';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE userpermission RENAME VALUE 'READ_WRITE' TO 'read_write';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'READ_ONLY' TO 'read_only';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'READ_UPLOAD' TO 'read_upload';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))
		op.execute(sa.text("""
			DO $$ BEGIN
				ALTER TYPE apikeypermission RENAME VALUE 'READ_WRITE' TO 'read_write';
			EXCEPTION WHEN invalid_parameter_value THEN NULL;
			END $$
		"""))

# vim: ts=4 sw=4 noet
