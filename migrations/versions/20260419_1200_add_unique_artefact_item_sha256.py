"""Add unique constraint on (item_id, sha256) in artefacts table

Prevents two concurrent uploads of the same file to the same item from
creating duplicate records when they race past the application-level
duplicate check.  NULL sha256 values are never considered equal in SQL,
so artefacts whose hash has not yet been computed are unaffected.

If duplicate (item_id, sha256) rows already exist in the database (from
races before this constraint existed), the upgrade removes them first,
keeping the earliest row (lowest id) in each duplicate group and deleting
all dependents of the removed rows in FK order.

Revision ID: 000069e53b12
Revises: 000069d43024
Create Date: 2026-04-19
"""
import sqlalchemy as sa
from alembic import op

revision = '000069e53b12'
down_revision = '000069d43024'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # Collect IDs of duplicate artefacts to remove.  For each (item_id, sha256)
    # group that has more than one row we keep the earliest (MIN id) and delete
    # the rest.  Rows with NULL sha256 are never duplicates in SQL and are left
    # untouched.
    bind.execute(sa.text("""
        CREATE TEMP TABLE _dupe_artefact_ids AS
        SELECT id FROM artefacts
        WHERE sha256 IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM artefacts
              WHERE sha256 IS NOT NULL
              GROUP BY item_id, sha256
          )
    """))

    dupe_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM _dupe_artefact_ids")
    ).scalar()

    if dupe_count:
        # Cascade deletes manually (no DB-level ON DELETE CASCADE on these FKs).

        # Partition-level children
        bind.execute(sa.text("""
            CREATE TEMP TABLE _dupe_partition_ids AS
            SELECT id FROM partitions
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            CREATE TEMP TABLE _dupe_file_ids AS
            SELECT id FROM extracted_files
            WHERE partition_id IN (SELECT id FROM _dupe_partition_ids)
        """))

        # Nullify self-referential parent_file_id before deleting files
        bind.execute(sa.text("""
            UPDATE extracted_files SET parent_file_id = NULL
            WHERE parent_file_id IN (SELECT id FROM _dupe_file_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM extracted_file_restrictions
            WHERE extracted_file_id IN (SELECT id FROM _dupe_file_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM extracted_files
            WHERE id IN (SELECT id FROM _dupe_file_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM recognised_products
            WHERE partition_id IN (SELECT id FROM _dupe_partition_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM partitions
            WHERE id IN (SELECT id FROM _dupe_partition_ids)
        """))

        # Nullify derived_from_analysis_id on artefacts derived from analyses
        # that belong to the duplicates, then delete those analyses.
        bind.execute(sa.text("""
            UPDATE artefacts SET derived_from_analysis_id = NULL
            WHERE derived_from_analysis_id IN (
                SELECT id FROM analyses
                WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
            )
        """))

        bind.execute(sa.text("""
            DELETE FROM analyses
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        # Direct artefact-level dependents
        bind.execute(sa.text("""
            DELETE FROM artefact_tags
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM artefact_protection
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM artefact_mastering
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM riscos_modules
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM artefact_restrictions
            WHERE artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        # Nullify parent_artefact_id on any derived artefacts before deleting
        bind.execute(sa.text("""
            UPDATE artefacts SET parent_artefact_id = NULL
            WHERE parent_artefact_id IN (SELECT id FROM _dupe_artefact_ids)
        """))

        bind.execute(sa.text("""
            DELETE FROM artefacts
            WHERE id IN (SELECT id FROM _dupe_artefact_ids)
        """))

    op.create_unique_constraint(
        'uq_artefact_item_sha256',
        'artefacts',
        ['item_id', 'sha256'],
    )


def downgrade():
    op.drop_constraint('uq_artefact_item_sha256', 'artefacts', type_='unique')

# vim: ts=4 sw=4 et
