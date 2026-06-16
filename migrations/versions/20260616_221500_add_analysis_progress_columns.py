"""Add live-progress columns to analyses

Long-running analyses (FILE_EXTRACTION / ARCHIVE_EXTRACT hashing thousands of
files, CLEANUP deleting hundreds of storage keys, ...) previously showed only
an opaque spinner, and "stale" detection keyed solely off started_at, forcing
STALE_JOB_TIMEOUT_SECONDS above the longest expected run.

These four nullable columns let the worker report structured progress
(message + current/total bar) without overloading the result `summary`, and
progress_updated_at is a heartbeat timestamp so an actively-progressing job is
no longer mistaken for a stuck one.

Revision ID: 00006a3194e4
Revises: 00006a31c878
Create Date: 2026-06-16 18:24:36 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a3194e4"
down_revision = "00006a31c878"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('analyses', sa.Column('progress_message', sa.Text(), nullable=True))
    op.add_column('analyses', sa.Column('progress_current', sa.Integer(), nullable=True))
    op.add_column('analyses', sa.Column('progress_total', sa.Integer(), nullable=True))
    op.add_column('analyses', sa.Column('progress_updated_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('analyses', 'progress_updated_at')
    op.drop_column('analyses', 'progress_total')
    op.drop_column('analyses', 'progress_current')
    op.drop_column('analyses', 'progress_message')

# vim: ts=4 sw=4 et
