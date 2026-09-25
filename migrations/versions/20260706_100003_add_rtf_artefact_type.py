"""Add RTF artefact type

Rich Text Format (.rtf) documents get a first-class artefact type so uploads
are classified directly and routed through FORMAT_CONVERT, which extracts their
text (via unrtf) for full-text search and a text view.

Revision ID: 00006a4bb48d
Revises: 00006a4bb486
Create Date: 2026-07-06 10:00:03 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4bb48d"
down_revision = "00006a4bb486"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'RTF'"
            ))


def downgrade():
    """Remap RTF artefacts to UNKNOWN (see the MS_EXCEL migration)."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'RTF'"
    ))

# vim: ts=4 sw=4 et
