"""Add DETECT_TRACK_DENSITY to analysistype enum

Revision ID: 000069e9644a
Revises: 000069e60a13
Create Date: 2026-04-21
"""
import sqlalchemy as sa
from alembic import op

revision = '000069e9644a'
down_revision = '000069e60a13'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
# env.py uses transaction_per_migration=True, so we opt out here.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'DETECT_TRACK_DENSITY'"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'DETECT_TRACK_DENSITY'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'DETECT_TRACK_DENSITY'"))

# vim: ts=4 sw=4 et
