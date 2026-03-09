"""Widen partitions.gnu_file_type from VARCHAR(256) to Text

file(1) output can exceed 256 characters for complex image types.

Revision ID: a1b2c3d4e5f6
Revises: b2e8f4a1c9d3
Create Date: 2026-03-09 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'b2e8f4a1c9d3'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('partitions', 'gnu_file_type',
                    existing_type=sa.String(256),
                    type_=sa.Text(),
                    existing_nullable=True)


def downgrade():
    # Truncate to 256 chars to avoid data loss errors on downgrade
    op.execute(sa.text(
        "UPDATE partitions SET gnu_file_type = LEFT(gnu_file_type, 256) "
        "WHERE LENGTH(gnu_file_type) > 256"
    ))
    op.alter_column('partitions', 'gnu_file_type',
                    existing_type=sa.Text(),
                    type_=sa.String(256),
                    existing_nullable=True)

# vim: ts=4 sw=4 et
