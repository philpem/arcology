"""Add artefact_tags association table

Extends tag support to artefacts using the same shared tag pool as items.

Revision ID: 000069ae92b3
Revises: 000069ae3f4e
Create Date: 2026-03-09 09:28:19.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069ae92b3'
down_revision = '000069ae3f4e'
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
