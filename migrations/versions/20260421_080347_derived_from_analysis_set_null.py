"""Fix cascade on derived_from_analysis_id: ON DELETE SET NULL

Prevents orphaned grandchild artefacts (HFE, RAW_SECTOR, etc.) when a parent
analysis chain is cleaned up.  Without ON DELETE SET NULL, deleting an Analysis
that artefacts reference via derived_from_analysis_id raised a FK RESTRICT error
in PostgreSQL, silently aborting the ORM cascade and leaving those artefacts in
the database.

Revision ID: 000069e72f63
Revises: 000069e66a3f
Create Date: 2026-04-21
"""
import sqlalchemy as sa
from alembic import op

revision = '000069e72f63'
down_revision = '000069e66a3f'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        'fk_artefacts_derived_from_analysis', 'artefacts', type_='foreignkey'
    )
    op.create_foreign_key(
        'fk_artefacts_derived_from_analysis',
        'artefacts', 'analyses',
        ['derived_from_analysis_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_artefacts_derived_from_analysis', 'artefacts', type_='foreignkey'
    )
    op.create_foreign_key(
        'fk_artefacts_derived_from_analysis',
        'artefacts', 'analyses',
        ['derived_from_analysis_id'], ['id'],
    )

# vim: ts=4 sw=4 et
