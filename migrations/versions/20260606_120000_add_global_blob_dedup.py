"""Add global upload and output blob deduplication

Revision ID: 00006a25a2c0
Revises: 00006a21fc7c
Create Date: 2026-06-06
"""

import sqlalchemy as sa
from alembic import op

revision = "00006a25a2c0"
down_revision = "00006a21fc7c"
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
        sa.Column("created_at", sa.DateTime(), nullable=True),
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
        bind.execute(sa.text(f"""
            INSERT INTO {table} (file_size, sha256, md5, storage_path, created_at)
            SELECT file_size, sha256, MIN(md5), MIN(storage_path), MIN(created_at)
            FROM artefacts
            WHERE storage_directory = :directory
              AND file_size IS NOT NULL
              AND sha256 IS NOT NULL
            GROUP BY file_size, sha256
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
