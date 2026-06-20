"""Reconcile denormalised counters that drifted from their source rows

Two stored counters could diverge from the rows they summarise (issue #637),
the same denormalisation class as the removed ``is_known`` column:

* ``partitions.total_files`` was seeded at partition registration *and*
  incremented per file batch, double-counting every extracted listing.
* ``hash_databases.file_count`` was decremented only on single-known-file
  deletes; deleting a whole product bulk-removed its ``known_files`` without
  adjusting the count, leaving it overcounting.

The code paths are fixed to keep both in step going forward (total_files is
recomputed alongside unique_files; file_count is recomputed after a product
delete).  This migration corrects the historical drift by recomputing each
counter from the actual rows.

Revision ID: 00006a36544e
Revises: 00006a364e90
Create Date: 2026-06-20 08:50:22.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a36544e'
down_revision = '00006a364e90'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "UPDATE partitions SET total_files = ("
        "  SELECT COUNT(*) FROM extracted_files"
        "  WHERE extracted_files.partition_id = partitions.id"
        ")"
    ))
    op.execute(sa.text(
        "UPDATE hash_databases SET file_count = ("
        "  SELECT COUNT(*) FROM known_files"
        "  WHERE known_files.database_id = hash_databases.id"
        ")"
    ))


def downgrade():
    # Data correction only — the previous (drifted) values are not recoverable
    # and would not be desirable to restore, so the downgrade is a no-op.
    pass


# vim: ts=4 sw=4 et
