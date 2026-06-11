"""Add global upload and output blob deduplication

Revision ID: 00006a25a2c0
Revises: 00006a2a2054
Create Date: 2026-06-11
"""

import sqlalchemy as sa
from alembic import op

revision = "00006a25a2c0"
down_revision = "00006a2a2054"
branch_labels = None
depends_on = None


def _create_blob_table(name, unique_name):
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=True),
        sa.Column("storage_path", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path", name=f"uq_{name}_storage_path"),
        sa.UniqueConstraint("file_size", "sha256", name=unique_name),
    )


def upgrade():
    _create_blob_table("upload_blobs", "uq_upload_blob_size_sha256")
    _create_blob_table("output_blobs", "uq_output_blob_size_sha256")

    with op.batch_alter_table("artefacts") as batch:
        batch.add_column(sa.Column("upload_blob_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("output_blob_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_artefacts_upload_blob_id", "upload_blobs",
            ["upload_blob_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_artefacts_output_blob_id", "output_blobs",
            ["output_blob_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_index("ix_artefacts_upload_blob_id", ["upload_blob_id"])
        batch.create_index("ix_artefacts_output_blob_id", ["output_blob_id"])
        batch.create_check_constraint(
            "ck_artefact_at_most_one_blob",
            "NOT (upload_blob_id IS NOT NULL AND output_blob_id IS NOT NULL)",
        )
        batch.drop_constraint("uq_artefact_item_sha256", type_="unique")

    bind = op.get_bind()
    for directory, table, column in (
        ("UPLOADS", "upload_blobs", "upload_blob_id"),
        ("OUTPUTS", "output_blobs", "output_blob_id"),
    ):
        # The row with the smallest id (oldest artefact) per (file_size, sha256)
        # becomes the canonical blob, so the original upload wins over later
        # re-uploads of the same content.  MIN(id) per group is portable SQL
        # (works on both PostgreSQL and SQLite dev databases).
        bind.execute(sa.text(f"""
            INSERT INTO {table} (file_size, sha256, md5, storage_path, created_at)
            SELECT file_size, sha256, md5, storage_path, created_at
            FROM artefacts
            WHERE id IN (
                SELECT MIN(id) FROM artefacts
                WHERE storage_directory = :directory
                  AND file_size IS NOT NULL
                  AND sha256 IS NOT NULL
                GROUP BY file_size, sha256
            )
        """), {"directory": directory})
        bind.execute(sa.text(f"""
            UPDATE artefacts
            SET {column} = (
                SELECT b.id FROM {table} b
                WHERE b.file_size = artefacts.file_size
                  AND b.sha256 = artefacts.sha256
            )
            WHERE storage_directory = :directory
              AND file_size IS NOT NULL
              AND sha256 IS NOT NULL
        """), {"directory": directory})

    op.create_index(
        "ix_extracted_files_sha256_size",
        "extracted_files",
        ["sha256", "file_size"],
    )


def downgrade():
    # This downgrade is effectively one-way in production.  The new design
    # intentionally allows multiple artefacts with the same SHA-256 on the same
    # item (e.g. two curators independently uploading the same disk image).  Once
    # any such pair exists the check below fires and the downgrade cannot proceed
    # without manually removing the duplicates.  Treat this migration as permanent
    # for any deployment where curators have been active since it was applied.
    duplicate = op.get_bind().execute(sa.text("""
        SELECT item_id, sha256, COUNT(*) AS duplicate_count
        FROM artefacts
        WHERE sha256 IS NOT NULL
        GROUP BY item_id, sha256
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade global blob deduplication while duplicate artefacts "
            f"exist in item {duplicate.item_id} for SHA-256 {duplicate.sha256}. "
            "Move or remove same-item duplicates before retrying the downgrade."
        )

    op.drop_index("ix_extracted_files_sha256_size", table_name="extracted_files")
    with op.batch_alter_table("artefacts") as batch:
        batch.create_unique_constraint(
            "uq_artefact_item_sha256", ["item_id", "sha256"]
        )
        batch.drop_constraint("ck_artefact_at_most_one_blob", type_="check")
        batch.drop_index("ix_artefacts_output_blob_id")
        batch.drop_index("ix_artefacts_upload_blob_id")
        batch.drop_constraint("fk_artefacts_output_blob_id", type_="foreignkey")
        batch.drop_constraint("fk_artefacts_upload_blob_id", type_="foreignkey")
        batch.drop_column("output_blob_id")
        batch.drop_column("upload_blob_id")
    op.drop_table("output_blobs")
    op.drop_table("upload_blobs")
