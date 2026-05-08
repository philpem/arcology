"""Add ARMLOCK_REMOVE analysis type

Revision ID: 000069c4322f
Revises: 000069c48529
Create Date: 2026-03-25

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069c4322f'
down_revision = '000069c48529'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
# This flag tells Alembic to run this migration outside a transaction block.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'ARMLOCK_REMOVE'"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'ARMLOCK_REMOVE'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'ARMLOCK_REMOVE'"))

# vim: ts=4 sw=4 et
