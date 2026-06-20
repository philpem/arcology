"""Reconcile partitions.total_files with its source rows

``partitions.total_files`` could diverge from the rows it summarises (issue
#637), the same denormalisation class as the removed ``is_known`` column: it was
seeded at partition registration *and* incremented per file batch, so every
extracted listing was double-counted.

The code is fixed to keep it in step going forward (the incremental counter is
the sole owner, and total_files is recomputed alongside unique_files on rescan).
This migration corrects the historical drift by recomputing it from the rows.

(``hash_databases.file_count`` had the same problem but is converted to a
derived value with no stored column in the following migration, so it needs no
data correction here.)

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


def downgrade():
    # Data correction only — the previous (drifted) values are not recoverable
    # and would not be desirable to restore, so the downgrade is a no-op.
    pass


# vim: ts=4 sw=4 et
