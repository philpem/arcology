"""Rename restrictiontype enum: NSFW -> EXPLICIT, remove EXPORT_CONTROL

The Python RestrictionType enum was updated to rename NSFW to EXPLICIT
and remove EXPORT_CONTROL, but the PostgreSQL enum type still has the
old values.  This migration renames the value in-place (PostgreSQL 10+).

For fresh installs where the base migration already created EXPLICIT
instead of NSFW, this is a no-op.

Revision ID: 000069c45b2d
Revises: 000069c2d776
Create Date: 2026-03-25
"""
import sqlalchemy as sa
from alembic import op

revision = '000069c45b2d'
down_revision = '000069c2d776'
branch_labels = None
depends_on = None

# Non-transactional DDL required for ALTER TYPE ... RENAME VALUE
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # Rename NSFW -> EXPLICIT directly.  RENAME VALUE is atomic and does
    # not require adding a new value first, so there is no "unsafe new
    # enum value" problem.  If NSFW doesn't exist (fresh install where
    # the base migration already used EXPLICIT), this is a no-op.
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum
                WHERE enumtypid = 'restrictiontype'::regtype
                  AND enumlabel = 'NSFW'
            ) THEN
                ALTER TYPE restrictiontype RENAME VALUE 'NSFW' TO 'EXPLICIT';
            END IF;
        END $$
    """))

    # EXPORT_CONTROL cannot be removed from a PostgreSQL enum, but the
    # Python enum no longer has it so SQLAlchemy will never write it.
    # Existing rows (if any) are left as-is.


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # Rename back: EXPLICIT -> NSFW
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum
                WHERE enumtypid = 'restrictiontype'::regtype
                  AND enumlabel = 'EXPLICIT'
            ) THEN
                ALTER TYPE restrictiontype RENAME VALUE 'EXPLICIT' TO 'NSFW';
            END IF;
        END $$
    """))

# vim: ts=4 sw=4 et
