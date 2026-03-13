"""Add unique constraint on artefact (derived_from_analysis_id, storage_path)

Prevents duplicate derived artefacts being created for the same analysis run
and output file path, e.g. when a worker retries a produce-artefact call after
a network timeout.  NULL values in derived_from_analysis_id are not considered
equal in SQL, so original (non-derived) artefacts are unaffected.

Revision ID: 000069b0e773
Revises: 000069ae92b3
Create Date: 2026-03-11 03:54:27.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069b0e773'
down_revision = '000069ae92b3'
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
