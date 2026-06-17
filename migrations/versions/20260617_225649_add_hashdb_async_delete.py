"""Add HASHDB_DELETE analysis type and hash_databases.is_deleting

Deleting a large hash database is now offloaded to a worker-driven
bounded-step HASHDB_DELETE job.  The web request marks the database
is_deleting=True (and is_active=False), so it drops out of matching and
listings immediately while the worker drains its rows in small batches.

Revision ID: 00006a332631
Revises: 00006a302000
Create Date: 2026-06-17 22:56:49
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a332631'
down_revision = '00006a302000'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction; autocommit_block()
        # commits the current per-migration transaction, switches to AUTOCOMMIT,
        # executes the DDL, then restores the original isolation level.
        with op.get_context().autocommit_block():
            op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'HASHDB_DELETE'"))
    op.add_column(
        'hash_databases',
        sa.Column('is_deleting', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    bind = op.get_bind()
    # PostgreSQL cannot remove an enum value; delete the rows that use it so the
    # ORM never reads a value absent from the Python enum (see CLAUDE.md).
    # HASHDB_DELETE jobs never produce artefacts, but null any back-references
    # defensively before deleting, mirroring the established pattern.
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("""
            UPDATE artefacts SET derived_from_analysis_id = NULL
            WHERE derived_from_analysis_id IN (
                SELECT id FROM analyses WHERE analysis_type = 'HASHDB_DELETE'
            )
        """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'HASHDB_DELETE'"))
    op.drop_column('hash_databases', 'is_deleting')

# vim: ts=4 sw=4 et
