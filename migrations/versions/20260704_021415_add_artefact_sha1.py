"""Add sha1 column to artefacts

Artefacts already store MD5 and SHA-256; SHA-1 is added so all three common
digests are available on the artefact view (and, later, searchable like the
per-file hashes).  The column is nullable and populated by the worker's
CHECKSUM_COMPUTE analysis (mirroring the existing TLSH plumbing); existing rows
are filled in by ``flask backfill-artefact-sha1``.

Revision ID: 00006a486c77
Revises: 00006a486a18
Create Date: 2026-07-04

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a486c77'
down_revision = '00006a486a18'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('artefacts', sa.Column('sha1', sa.String(length=40), nullable=True))


def downgrade():
    op.drop_column('artefacts', 'sha1')
