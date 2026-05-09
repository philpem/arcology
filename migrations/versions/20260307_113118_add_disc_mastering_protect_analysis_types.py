"""Add disc_mastering_detect and disc_protection_detect analysis types

Revision ID: 000069ac0c86
Revises: 000069a1137d
Create Date: 2026-03-07 11:31:18.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069ac0c86'
down_revision = '000069a1137d'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
# This flag tells Alembic to run this migration outside a transaction block.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_MASTERING_DETECT'"
        ))
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DISC_PROTECTION_DETECT'"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # Remove rows using enum values no longer in the Python enum so the ORM
    # doesn't raise LookupError when materialising Analysis objects.
    # NULL out derived_from_analysis_id first; the FK may not have ON DELETE
    # SET NULL at this point in the downgrade chain.
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses
            WHERE analysis_type IN ('DISC_MASTERING_DETECT', 'DISC_PROTECTION_DETECT')
        )
    """))
    op.execute(sa.text(
        "DELETE FROM analyses"
        " WHERE analysis_type IN ('DISC_MASTERING_DETECT', 'DISC_PROTECTION_DETECT')"
    ))

# vim: ts=4 sw=4 et
