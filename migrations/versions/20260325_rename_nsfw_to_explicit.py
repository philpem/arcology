"""Rename restrictiontype enum: NSFW -> EXPLICIT, remove EXPORT_CONTROL

The Python RestrictionType enum was updated to rename NSFW to EXPLICIT
and remove EXPORT_CONTROL, but the PostgreSQL enum type still has the
old values.  This migration renames the value in-place (PostgreSQL 10+)
and adds the new EXPLICIT value for databases that were created with
the updated migration.

Revision ID: 000069c45b2d
Revises: 000069c2d776
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa

revision = '000069c45b2d'
down_revision = '000069c2d776'
branch_labels = None
depends_on = None

# Non-transactional DDL: ALTER TYPE ... ADD VALUE cannot run in a transaction
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # Add EXPLICIT if it doesn't exist yet (covers fresh installs where
    # the base migration already has EXPLICIT instead of NSFW)
    op.execute(sa.text(
        "ALTER TYPE restrictiontype ADD VALUE IF NOT EXISTS 'EXPLICIT'"
    ))

    # Rename NSFW -> EXPLICIT for databases created before the rename.
    # ALTER TYPE ... RENAME VALUE requires PostgreSQL 10+.
    # If NSFW doesn't exist (fresh install), this is a no-op.
    op.execute(sa.text("""
        DO $$ BEGIN
            -- Check if NSFW still exists as a value
            IF EXISTS (
                SELECT 1 FROM pg_enum
                WHERE enumtypid = 'restrictiontype'::regtype
                  AND enumlabel = 'NSFW'
            ) THEN
                -- Update any rows using the old value first
                UPDATE artefact_restrictions
                   SET restriction_type = 'EXPLICIT'
                 WHERE restriction_type = 'NSFW';
                UPDATE user_restriction_bypasses
                   SET restriction_type = 'EXPLICIT'
                 WHERE restriction_type = 'NSFW';
                UPDATE hash_databases
                   SET restriction_type = 'EXPLICIT'
                 WHERE restriction_type = 'NSFW';

                -- Rename the enum value
                ALTER TYPE restrictiontype RENAME VALUE 'NSFW' TO '_NSFW_DEPRECATED';
            END IF;
        END $$
    """))

    # EXPORT_CONTROL cannot be removed from a PostgreSQL enum, but we can
    # ensure no rows reference it.  The Python enum no longer has it, so
    # SQLAlchemy will never write it.  Existing rows are left as-is (there
    # should be none since the feature is new).


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # Rename back: _NSFW_DEPRECATED -> NSFW (if it was renamed)
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum
                WHERE enumtypid = 'restrictiontype'::regtype
                  AND enumlabel = '_NSFW_DEPRECATED'
            ) THEN
                UPDATE artefact_restrictions
                   SET restriction_type = '_NSFW_DEPRECATED'
                 WHERE restriction_type = 'EXPLICIT';
                UPDATE user_restriction_bypasses
                   SET restriction_type = '_NSFW_DEPRECATED'
                 WHERE restriction_type = 'EXPLICIT';
                UPDATE hash_databases
                   SET restriction_type = '_NSFW_DEPRECATED'
                 WHERE restriction_type = 'EXPLICIT';

                ALTER TYPE restrictiontype RENAME VALUE '_NSFW_DEPRECATED' TO 'NSFW';
            END IF;
        END $$
    """))

# vim: ts=4 sw=4 et
