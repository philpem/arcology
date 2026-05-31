"""Add STAFF value to userpermission enum

Revision ID: 00006a1a7977
Revises: 00006a17b70e
Create Date: 2026-05-30

Adds a STAFF tier between READ_WRITE and admin to UserPermission.
STAFF users have full read/write access plus taxonomy and hash-DB management;
below admin (no user management or system configuration).

Downgrade remaps any STAFF users to READ_WRITE so the ORM never sees an
unknown enum value after the Python-side enum value is removed.

PostgreSQL cannot remove enum values once added — downgrade preserves the DB
enum value but ensures no rows reference it.
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a1a7977'
down_revision = '00006a17b70e'
branch_labels = None
depends_on = None

autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE userpermission ADD VALUE IF NOT EXISTS 'STAFF'"))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE \"user\" SET permission = 'READ_WRITE' WHERE permission = 'STAFF'"
    ))

# vim: ts=4 sw=4 et
