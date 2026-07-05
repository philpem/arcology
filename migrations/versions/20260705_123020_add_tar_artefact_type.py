"""Add TAR artefact type

Uncompressed ``.tar`` archives were previously typed UNKNOWN at upload (only
``.tar.gz``/``.tgz`` had a first-class TARGZ type), so a bare tar routed
through FORMAT_IDENTIFY rather than straight to ARCHIVE_EXTRACT.  With a
first-class type, ``.tar`` uploads are classified directly, the artefact badge
is correct, and the Acorn default-filetype hint applies to them.

Revision ID: 00006a4a4e5c
Revises: 00006a48d082
Create Date: 2026-07-05 12:30:20 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4a4e5c"
down_revision = "00006a48d082"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ADD VALUE cannot run inside a transaction;
        # autocommit_block() commits the surrounding transaction first.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE artefacttype ADD VALUE IF NOT EXISTS 'TAR'"
            ))


def downgrade():
    """Remap TAR artefacts to UNKNOWN.

    PostgreSQL cannot remove an enum value, and the ORM crashes with
    LookupError on rows holding a value absent from the Python enum, so
    rows must be cleaned up.  Remapping to UNKNOWN preserves the uploaded
    files and their analyses; UNKNOWN artefacts simply re-route through
    FORMAT_IDENTIFY on re-analysis — exactly the pre-TAR behaviour for
    uncompressed tar uploads.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(
        "UPDATE artefacts SET artefact_type = 'UNKNOWN' "
        "WHERE artefact_type = 'TAR'"
    ))

# vim: ts=4 sw=4 et
