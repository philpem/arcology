"""Add LHA artefact type

LHA / LZH archives (.lha / .lzh) get a first-class artefact type so uploads are
classified directly and routed through ARCHIVE_EXTRACT, which unpacks them with
lha (lhasa) and registers the extracted files.

Revision ID: 00006a4bb49b
Revises: 00006a4bb494
Create Date: 2026-07-06 10:00:05 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4bb49b"
down_revision = "00006a4bb494"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'LHA'"
            ))


def downgrade():
    """Remap LHA artefacts to UNKNOWN (see the MS_EXCEL migration)."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'LHA'"
    ))

# vim: ts=4 sw=4 et
