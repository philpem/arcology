"""Widen extracted_files.extension from VARCHAR(20) to VARCHAR(255)

Files with GUID-style extensions (e.g. Windows shell namespace folders like
'Internet Mail.{89292102-4755-11cf-9DC2-00AA006C2B84}') produce extensions
longer than 20 characters, causing StringDataRightTruncation errors on insert.

Revision ID: 000069c48529
Revises: 000069c45b2d
Create Date: 2026-03-26
"""
from alembic import op
import sqlalchemy as sa

revision = '000069c48529'
down_revision = '000069c45b2d'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('extracted_files', 'extension',
                    type_=sa.String(255),
                    existing_nullable=True)


def downgrade():
    op.alter_column('extracted_files', 'extension',
                    type_=sa.String(20),
                    existing_nullable=True)

# vim: ts=4 sw=4 et
