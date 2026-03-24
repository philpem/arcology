"""Add indexes on Analysis.status and Analysis.created_at for query performance

Revision ID: 000069c2d753
Revises: 000069b4c8ec
Create Date: 2026-03-24

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '000069c2d753'
down_revision = '000069b4c8ec'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_analyses_status', 'analyses', ['status'])
    op.create_index('ix_analyses_created_at', 'analyses', ['created_at'])


def downgrade():
    op.drop_index('ix_analyses_created_at', table_name='analyses')
    op.drop_index('ix_analyses_status', table_name='analyses')


# vim: ts=4 sw=4 et
