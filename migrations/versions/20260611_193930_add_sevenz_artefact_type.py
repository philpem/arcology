"""Add SEVENZ artefact type

7-Zip archives were previously typed UNKNOWN at upload (no ArtefactType
member existed), relying on FORMAT_IDENTIFY's magic-byte sniff to route
them to ARCHIVE_EXTRACT.  With a first-class type, '.7z' uploads are
classified directly and the artefact badge is correct.

Revision ID: 00006a2b0ef2
Revises: 00006a25a2c0
Create Date: 2026-06-11 19:39:30 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a2b0ef2"
down_revision = "00006a25a2c0"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'SEVENZ'"
            ))


def downgrade():
    """Remap SEVENZ artefacts to UNKNOWN.

    PostgreSQL cannot remove an enum value, and the ORM crashes with
    LookupError on rows holding a value absent from the Python enum, so
    rows must be cleaned up.  Unlike earlier artefact-type migrations
    that cascade-deleted the artefacts, remapping to UNKNOWN preserves
    the uploaded files and their analyses; UNKNOWN artefacts simply
    re-route through FORMAT_IDENTIFY on re-analysis — exactly the
    pre-SEVENZ behaviour for 7-Zip uploads.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'SEVENZ'"
    ))

# vim: ts=4 sw=4 et
