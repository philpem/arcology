"""Add artefact_tags association table

Extends tag support to artefacts using the same shared tag pool as items.

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7a8b9
Create Date: 2026-03-09 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'artefact_tags',
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ),
        sa.PrimaryKeyConstraint('artefact_id', 'tag_id'),
    )


def downgrade():
    op.drop_table('artefact_tags')

# vim: ts=4 sw=4 et
