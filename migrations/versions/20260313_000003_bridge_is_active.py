"""Bridge migration: retains revision 000069b45216 in the chain

This is a no-op stub.  The revision ID 000069b45216 was applied to databases
that were running the development branch before the four incremental migrations
were consolidated into 20260313_hash_database_product_recognition.py.

Retaining this ID here lets those databases run ``flask db upgrade`` without
hitting "Can't locate revision identified by '000069b45216'".  The actual DDL
is handled by the following migration (000069b47df0), which is written to be
idempotent and skips objects that already exist.

Revision ID: 000069b45216
Revises: 000069b0e773
Create Date: 2026-03-13

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069b45216'
down_revision = '000069b0e773'
branch_labels = None
depends_on = None


def upgrade():
    # No DDL — the consolidated migration (000069b47df0) handles everything
    # and is written to skip objects that already exist.
    pass


def downgrade():
    pass

# vim: ts=4 sw=4 et
