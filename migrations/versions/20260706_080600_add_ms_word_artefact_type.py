"""Add MS_WORD artefact type

Microsoft Word documents (.doc legacy binary, .docx OOXML) get a first-class
artefact type so uploads are classified directly and routed through
FORMAT_CONVERT, which extracts their plain text for full-text search
(search_documents) and a text viewer.

Revision ID: 00006a48d1bd
Revises: 00006a4a48dd
Create Date: 2026-07-04 09:26:21 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a48d1bd"
down_revision = "00006a4a48dd"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'MS_WORD'"
            ))


def downgrade():
    """Remap MS_WORD artefacts to UNKNOWN.

    PostgreSQL cannot remove an enum value, and the ORM crashes with
    LookupError on rows holding a value absent from the Python enum, so
    rows must be cleaned up.  Remapping to UNKNOWN (rather than deleting)
    preserves the uploaded files and their analyses; an UNKNOWN artefact
    simply re-routes through FORMAT_IDENTIFY on re-analysis.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'MS_WORD'"
    ))

# vim: ts=4 sw=4 et
