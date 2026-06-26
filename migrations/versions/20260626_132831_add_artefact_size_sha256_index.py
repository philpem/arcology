"""Add (file_size, sha256) index on artefacts

The storage deduplication stats (/storage) and `flask dedup-artefacts` GROUP BY
(file_size, sha256) over the whole artefacts table, and the /storage
duplicate-group drill-down (duplicate_group_instances) does a
``WHERE file_size = ? AND sha256 = ?`` point lookup.  Artefact had no index
covering these columns (unlike extracted_files / known_files / the blob tables),
so those queries fell back to a sequential scan.  Add a composite index.

Revision ID: 00006a3e7e7f
Revises: 00006a3db85a
Create Date: 2026-06-26 13:28:31 UTC
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a3e7e7f"
down_revision = "00006a3db85a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_artefacts_size_sha256", "artefacts", ["file_size", "sha256"]
    )


def downgrade():
    op.drop_index("ix_artefacts_size_sha256", table_name="artefacts")
