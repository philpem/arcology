"""Add CLEANUP analysis type; make analyses.artefact_id nullable

CLEANUP jobs carry storage keys to delete in their hints JSON.  Jobs queued
by bulk item deletion outlive their artefacts, so artefact_id must allow
NULL for this type.

Revision ID: 00006a2a2054
Revises: 00006a21fc7c
Create Date: 2026-06-11 02:41:38
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a2a2054'
down_revision = '00006a21fc7c'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'CLEANUP'"))
    op.alter_column('analyses', 'artefact_id',
                    existing_type=sa.Integer(),
                    nullable=True)


def downgrade():
    bind = op.get_bind()
    # PostgreSQL cannot remove an enum value; delete the rows that use it so
    # the ORM never reads a value absent from the Python enum (see CLAUDE.md).
    # CLEANUP analyses never produce artefacts, but null any back-references
    # defensively before deleting, mirroring the established pattern.
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("""
            UPDATE artefacts SET derived_from_analysis_id = NULL
            WHERE derived_from_analysis_id IN (
                SELECT id FROM analyses WHERE analysis_type = 'CLEANUP'
            )
        """))
        op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'CLEANUP'"))
    else:
        op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'CLEANUP'"))
    op.alter_column('analyses', 'artefact_id',
                    existing_type=sa.Integer(),
                    nullable=False)

# vim: ts=4 sw=4 et
