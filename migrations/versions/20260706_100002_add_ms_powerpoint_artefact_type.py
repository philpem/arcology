"""Add MS_POWERPOINT artefact type

Legacy binary Microsoft PowerPoint (.ppt) documents get a first-class artefact
type so uploads are classified directly and routed through FORMAT_CONVERT,
which extracts their slide text (via catppt) for full-text search and a view.

Revision ID: 00006a4bb486
Revises: 00006a4bb47f
Create Date: 2026-07-06 10:00:02 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4bb486"
down_revision = "00006a4bb47f"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'MS_POWERPOINT'"
            ))


def downgrade():
    """Remap MS_POWERPOINT artefacts to UNKNOWN (see the MS_EXCEL migration)."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'MS_POWERPOINT'"
    ))

# vim: ts=4 sw=4 et
