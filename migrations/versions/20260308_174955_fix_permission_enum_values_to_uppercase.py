"""Fix permission enum values to uppercase

Previous migration created userpermission and apikeypermission enum types
with lowercase values (read_only, read_write, read_upload), but SQLAlchemy
stores Python enum member names which are uppercase (READ_ONLY, READ_WRITE,
READ_UPLOAD). Rename the enum values in-place.

Revision ID: 000069adb6c3
Revises: 000069ac2032
Create Date: 2026-03-08 17:49:55.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069adb6c3'
down_revision = '000069ac2032'
branch_labels = None
depends_on = None

# ALTER TYPE RENAME VALUE cannot run inside a transaction in PostgreSQL.
# env.py uses transaction_per_migration=True; this flag opts out.
autocommit = True


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

# vim: ts=4 sw=4 et
