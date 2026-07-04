"""Add full-text search vectors for item/artefact name & description

Adds a PostgreSQL generated ``tsvector`` column (plus a GIN index) to ``items``
and ``artefacts``.  The vector weights the name/label ('A') above the
description ('B') so relevance ranking favours a name match.

These power ``ts_rank_cd`` ordering and ``ts_headline`` snippets on the free-text
search (see ``myapp/services/search.py``); the actual matching still uses the
existing trigram-backed ILIKE, so the result set is unchanged.  The column is
also the groundwork for document full-text search.

PostgreSQL-only: SQLite (tests / lightweight dev) has no tsvector type and keeps
the ILIKE path, so the whole migration is a no-op there.

Revision ID: 00006a48bb61
Revises: 00006a486c77
Create Date: 2026-07-04

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a48bb61'
down_revision = '00006a486c77'
branch_labels = None
depends_on = None


# (table, weighted tsvector expression) — name/label weighted 'A', description 'B'.
_VECTORS = {
    'items': (
        "setweight(to_tsvector('english', coalesce(name, '')), 'A') || "
        "setweight(to_tsvector('english', coalesce(description, '')), 'B')"
    ),
    'artefacts': (
        "setweight(to_tsvector('english', coalesce(label, '')), 'A') || "
        "setweight(to_tsvector('english', coalesce(description, '')), 'B')"
    ),
}


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    for table, expr in _VECTORS.items():
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN search_vector tsvector "
            f"GENERATED ALWAYS AS ({expr}) STORED"
        ))
        op.execute(sa.text(
            f"CREATE INDEX ix_{table}_search_vector "
            f"ON {table} USING GIN (search_vector)"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    for table in _VECTORS:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_search_vector"))
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS search_vector"))
