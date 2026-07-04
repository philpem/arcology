"""Add artefact hash and created_at indexes

Artefact hash columns (md5, sha1, sha256) had no standalone index — unlike
extracted_files / known_files, which index md5/sha1 — so a hash: search
(services/search._search_artefact_hashes) seq-scanned the artefacts table.  The
existing ix_artefacts_size_sha256 leads on file_size (for the dedup GROUP BY),
so it can't serve a sha256-only lookup.  created_at was likewise unindexed on
both items and artefacts, though the dashboard "recent items" list and the
item/artefact "uploaded" sort both ORDER BY created_at DESC.

Adds btree indexes on artefacts(md5), artefacts(sha1), artefacts(sha256),
artefacts(created_at) and items(created_at).

Revision ID: 00006a48bc25
Revises: 00006a48bb61
Create Date: 2026-07-04 07:54:13 UTC
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a48bc25"
down_revision = "00006a48bb61"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_artefacts_md5", "artefacts", ["md5"])
    op.create_index("ix_artefacts_sha1", "artefacts", ["sha1"])
    op.create_index("ix_artefacts_sha256", "artefacts", ["sha256"])
    op.create_index("ix_artefacts_created_at", "artefacts", ["created_at"])
    op.create_index("ix_items_created_at", "items", ["created_at"])


def downgrade():
    op.drop_index("ix_items_created_at", table_name="items")
    op.drop_index("ix_artefacts_created_at", table_name="artefacts")
    op.drop_index("ix_artefacts_sha256", table_name="artefacts")
    op.drop_index("ix_artefacts_sha1", table_name="artefacts")
    op.drop_index("ix_artefacts_md5", table_name="artefacts")
