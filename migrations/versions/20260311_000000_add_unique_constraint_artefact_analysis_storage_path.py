"""Add unique constraint on artefact (derived_from_analysis_id, storage_path)

Prevents duplicate derived artefacts being created for the same analysis run
and output file path, e.g. when a worker retries a produce-artefact call after
a network timeout.  NULL values in derived_from_analysis_id are not considered
equal in SQL, so original (non-derived) artefacts are unaffected.

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6
Create Date: 2026-03-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        'uq_artefact_analysis_storage_path',
        'artefacts',
        ['derived_from_analysis_id', 'storage_path'],
    )


def downgrade():
    op.drop_constraint(
        'uq_artefact_analysis_storage_path',
        'artefacts',
        type_='unique',
    )

# vim: ts=4 sw=4 et
