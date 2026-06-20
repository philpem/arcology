"""Drop the denormalised hash_databases.file_count column

``file_count`` mirrored ``COUNT(known_files WHERE database_id = ...)`` but was
maintained incrementally: incremented on import, decremented only on
single-known-file deletes, so a product delete (bulk-removing its known_files)
left it overcounting (issue #637).  It is now a derived ``column_property`` on
the ORM (a correlated COUNT subquery), so the stored column is removed and the
value can never drift.

Revision ID: 00006a365847
Revises: 00006a36544e
Create Date: 2026-06-20 09:07:19.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a365847'
down_revision = '00006a36544e'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('hash_databases', 'file_count')


def downgrade():
    op.add_column('hash_databases', sa.Column('file_count', sa.Integer(), nullable=True))
    # Re-derive the column from the rows it used to summarise.
    op.execute(sa.text(
        "UPDATE hash_databases SET file_count = ("
        "  SELECT COUNT(*) FROM known_files"
        "  WHERE known_files.database_id = hash_databases.id"
        ")"
    ))


# vim: ts=4 sw=4 et
