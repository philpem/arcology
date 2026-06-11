"""Add NSFW_SCAN to analysistype enum.

Revision ID: 00006a2b1cd8
Revises: 00006a314d69
Create Date: 2026-06-17 00:00:00 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a2b1cd8"
down_revision = "00006a314d69"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'NSFW_SCAN'"
            ))


def downgrade():
    """Delete NSFW_SCAN rows so the ORM never sees an unknown enum value.

    Null out derived_from_analysis_id references first — the FK may not
    have ON DELETE SET NULL at this point in the downgrade chain.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'NSFW_SCAN'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'NSFW_SCAN'"))

# vim: ts=4 sw=4 et
