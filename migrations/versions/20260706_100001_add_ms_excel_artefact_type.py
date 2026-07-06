"""Add MS_EXCEL artefact type

Legacy binary Microsoft Excel (.xls) documents get a first-class artefact type
so uploads are classified directly and routed through FORMAT_CONVERT, which
extracts their cell text (via xls2csv) for full-text search and a text view.

Revision ID: 00006a4bb47f
Revises: 00006a48d1bd
Create Date: 2026-07-06 10:00:01 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4bb47f"
down_revision = "00006a48d1bd"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'MS_EXCEL'"
            ))


def downgrade():
    """Remap MS_EXCEL artefacts to UNKNOWN.

    PostgreSQL cannot remove an enum value and the ORM crashes on rows holding
    a value absent from the Python enum, so remap (rather than delete) to
    preserve the uploaded files; an UNKNOWN artefact re-routes through
    FORMAT_IDENTIFY on re-analysis.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'MS_EXCEL'"
    ))

# vim: ts=4 sw=4 et
