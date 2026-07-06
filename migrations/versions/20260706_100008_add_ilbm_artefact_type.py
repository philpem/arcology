"""Add ILBM artefact type

Amiga IFF / ILBM bitmap images (.iff / .ilbm / .lbm) get a first-class artefact
type so uploads are classified directly and routed through FORMAT_CONVERT,
which renders them to PNG (via ImageMagick) for viewing.

Revision ID: 00006a4bb4b0
Revises: 00006a4bb4a9
Create Date: 2026-07-06 10:00:08 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4bb4b0"
down_revision = "00006a4bb4a9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'ILBM'"
            ))


def downgrade():
    """Remap ILBM artefacts to UNKNOWN (see the MS_EXCEL migration)."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'ILBM'"
    ))

# vim: ts=4 sw=4 et
