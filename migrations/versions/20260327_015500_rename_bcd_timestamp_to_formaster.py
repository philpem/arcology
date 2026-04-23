"""Rename mastering type bcd_timestamp to formaster

Revision ID: 000069c47146
Revises: 000069c2d753
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa

revision = '000069c47146'
down_revision = '000069c2d753'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text("UPDATE artefact_mastering SET mastering_type = 'formaster' "
                "WHERE mastering_type = 'bcd_timestamp'")
    )


def downgrade():
    op.execute(
        sa.text("UPDATE artefact_mastering SET mastering_type = 'bcd_timestamp' "
                "WHERE mastering_type = 'formaster'")
    )

# vim: ts=4 sw=4 et
