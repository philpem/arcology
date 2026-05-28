"""Add CHECK constraint requiring exactly one principal on item_shares

Revision ID: 00006a17b70e
Revises: 00006a17aed5
Create Date: 2026-05-28

Ensures every item_shares row has exactly one of user_id / group_id set,
preventing the degenerate (NULL, NULL) state that would silently grant no
access and would fail the NOT NULL semantic implied by the model.
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a17b70e'
down_revision = '00006a17aed5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_check_constraint(
        'ck_item_shares_exactly_one_principal',
        'item_shares',
        sa.text(
            "(user_id IS NOT NULL AND group_id IS NULL) OR "
            "(user_id IS NULL AND group_id IS NOT NULL)"
        ),
    )


def downgrade():
    op.drop_constraint('ck_item_shares_exactly_one_principal', 'item_shares')

# vim: ts=4 sw=4 et
