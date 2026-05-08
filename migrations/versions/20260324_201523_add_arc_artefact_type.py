"""Add ARC to ArtefactType enum

Revision ID: 000069c2f0db
Revises: 000069c2e953
Create Date: 2026-03-24
"""
import sqlalchemy as sa
from alembic import op

revision = '000069c2f0db'
down_revision = '000069c2e953'
branch_labels = None
depends_on = None

# Non-transactional DDL required for ALTER TYPE ... ADD VALUE
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ARC'"))


def _cascade_sql(type_names):
    """Build the FK-safe DELETE cascade for the given artefact type names.

    PostgreSQL DO blocks cannot accept bind parameters, so type names are
    inlined as quoted SQL literals.  Names are hardcoded enum identifiers
    (no user input) so this is safe.
    """
    types_in = ', '.join(f"'{name}'" for name in type_names)
    return f"""
    DO $$
    DECLARE _ids INTEGER[];
    BEGIN
        SELECT array_agg(id) INTO _ids FROM artefacts
            WHERE artefact_type IN ({types_in});
        IF _ids IS NULL THEN RETURN; END IF;

        UPDATE artefacts SET parent_artefact_id = NULL
            WHERE parent_artefact_id = ANY(_ids);
        UPDATE artefacts SET derived_from_analysis_id = NULL
            WHERE derived_from_analysis_id IN (
                SELECT id FROM analyses WHERE artefact_id = ANY(_ids));

        IF EXISTS (SELECT FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name = 'extracted_file_restrictions') THEN
            DELETE FROM extracted_file_restrictions
                WHERE extracted_file_id IN (
                    SELECT ef.id FROM extracted_files ef
                    JOIN partitions p ON ef.partition_id = p.id
                    WHERE p.artefact_id = ANY(_ids));
        END IF;

        DELETE FROM extracted_files
            WHERE partition_id IN (SELECT id FROM partitions WHERE artefact_id = ANY(_ids));
        DELETE FROM recognised_products
            WHERE partition_id IN (SELECT id FROM partitions WHERE artefact_id = ANY(_ids));
        DELETE FROM partitions WHERE artefact_id = ANY(_ids);
        DELETE FROM analyses WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_protection  WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_mastering   WHERE artefact_id = ANY(_ids);

        IF EXISTS (SELECT FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name = 'artefact_restrictions') THEN
            DELETE FROM artefact_restrictions WHERE artefact_id = ANY(_ids);
        END IF;

        IF EXISTS (SELECT FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name = 'riscos_modules') THEN
            DELETE FROM riscos_modules WHERE artefact_id = ANY(_ids);
        END IF;

        DELETE FROM artefact_tags WHERE artefact_id = ANY(_ids);
        DELETE FROM artefacts WHERE id = ANY(_ids);
    END $$
"""


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # Delete artefacts of this type and their children in FK-safe order.
    # The PG enum value itself cannot be removed, but no row will reference it
    # so the ORM will never encounter a LookupError.
    op.execute(sa.text(_cascade_sql(['ARC'])))

# vim: ts=4 sw=4 et
