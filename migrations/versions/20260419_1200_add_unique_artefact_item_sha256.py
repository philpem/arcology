"""Add unique constraint on (item_id, sha256) in artefacts table

Prevents two concurrent uploads of the same file to the same item from
creating duplicate records when they race past the application-level
duplicate check.  NULL sha256 values are never considered equal in SQL,
so artefacts whose hash has not yet been computed are unaffected.

Revision ID: 000069e53b12
Revises: 000069d43024
Create Date: 2026-04-19
"""
from alembic import op

revision = '000069e53b12'
down_revision = '000069d43024'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        'uq_artefact_item_sha256',
        'artefacts',
        ['item_id', 'sha256'],
    )


def downgrade():
    op.drop_constraint('uq_artefact_item_sha256', 'artefacts', type_='unique')

# vim: ts=4 sw=4 et
