"""Add HASH_RESCAN to analysistype enum

Revision ID: 00006a1d395f
Revises: 00006a1a7977
Create Date: 2026-06-01 07:56:33

"""
import sqlalchemy as sa
from alembic import op

revision = '00006a1d395f'
down_revision = '00006a1a7977'
branch_labels = None
depends_on = None

autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'HASH_RESCAN'"))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'HASH_RESCAN'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'HASH_RESCAN'"))
