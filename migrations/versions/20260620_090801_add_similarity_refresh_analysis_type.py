"""Add SIMILARITY_REFRESH analysis type

Adds the ``SIMILARITY_REFRESH`` value to the ``analysistype`` enum.  It backs a
worker-driven bounded job that recomputes one artefact's content-set similarity
off the synchronous request path (see myapp/services/similarity.py and the
``/artefacts/<uuid>/similarity-step`` endpoint).

Revision ID: 00006a3523b3
Revises: 00006a2f7086
Create Date: 2026-06-19 11:10:43.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a3523b3'
down_revision = '00006a2f7086'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'SIMILARITY_REFRESH'"
            ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # PostgreSQL cannot drop an enum value; delete rows using it so the ORM does
    # not crash with LookupError after a downgrade.  SIMILARITY_REFRESH jobs never
    # produce derived artefacts, but null any stray reference defensively first.
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'SIMILARITY_REFRESH'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'SIMILARITY_REFRESH'"))

# vim: ts=4 sw=4 et
