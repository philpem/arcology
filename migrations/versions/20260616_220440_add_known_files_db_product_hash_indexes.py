"""Add composite (database_id, product_id, hash) indexes on known_files

Speeds up the per-(database, product) duplicate check performed during hash
database import (_existing_known_file in myapp/blueprints/hashdb.py), which
filters on (database_id, product_id, md5/sha1) and could not use the
single-column md5/sha1 indexes efficiently. A sha256 composite index is added
for the same reason (sha256 previously had no index at all).

Revision ID: 00006a31c878
Revises: 00006a314d69
Create Date: 2026-06-16

"""
from alembic import op

revision = '00006a31c878'
down_revision = '00006a314d69'
branch_labels = None
depends_on = None


_INDEXES = (
    ('ix_known_files_db_product_md5', ['database_id', 'product_id', 'md5']),
    ('ix_known_files_db_product_sha1', ['database_id', 'product_id', 'sha1']),
    ('ix_known_files_db_product_sha256', ['database_id', 'product_id', 'sha256']),
)


def upgrade():
    for name, cols in _INDEXES:
        op.create_index(name, 'known_files', cols, unique=False)


def downgrade():
    for name, _cols in reversed(_INDEXES):
        op.drop_index(name, table_name='known_files')

# vim: ts=4 sw=4 et
