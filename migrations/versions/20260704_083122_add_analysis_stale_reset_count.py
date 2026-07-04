"""Add stale_reset_count to analyses

A "poison" analysis job — one that repeatedly crashes or hangs the worker
processing it — was previously re-queued forever: it would be claimed, stall,
be reset from RUNNING back to PENDING once it aged past STALE_JOB_TIMEOUT_SECONDS,
be re-claimed, and stall again.  Each cycle occupied a worker slot and logged a
fresh "Recovered 1 stale analysis job(s)" line with no way to break out.

This column counts how many times a job has been re-queued from stale.  Once it
reaches STALE_JOB_MAX_RETRIES the stale-reset dead-letters the job to FAILED
instead of re-queueing it, terminating the loop and surfacing the bad job.

NOT NULL with a server_default of 0 so pre-existing rows backfill cleanly.

Revision ID: 00006a48d082
Revises: 00006a48c0c8
Create Date: 2026-07-04 08:31:22 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a48d082"
down_revision = "00006a48c0c8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'analyses',
        sa.Column('stale_reset_count', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
    )


def downgrade():
    op.drop_column('analyses', 'stale_reset_count')

# vim: ts=4 sw=4 et
