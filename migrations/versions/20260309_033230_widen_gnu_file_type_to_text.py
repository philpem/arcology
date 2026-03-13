"""Widen partitions.gnu_file_type from VARCHAR(256) to Text

file(1) output can exceed 256 characters for complex image types.

Revision ID: 000069ae3f4e
Revises: 000069ae3f4d
Create Date: 2026-03-09 03:32:30.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '000069ae3f4e'
down_revision = '000069ae3f4d'
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
