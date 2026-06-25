"""Add worker_heartbeats table and User.can_prioritise_analyses.

Two unrelated-but-small schema additions for the analysis-queue fairness and
priority work:

  * ``worker_heartbeats`` — one row per live analysis worker (self-generated id,
    last-seen timestamp), used to size the heavy-job fairness cap to the live
    worker fleet (see myapp/services/analysis_queue.py).
  * ``user.can_prioritise_analyses`` — grants raising a re-analysis above the
    web-UI default priority (the Urgent tier).

Plain DDL only — no enum changes, so no autocommit block is needed.

Revision ID: 00006a3beadf
Revises: 00006a3b25dd
Create Date: 2026-06-24 14:34:07.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a3beadf'
down_revision = '00006a3b25dd'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column(
        'can_prioritise_analyses', sa.Boolean(), nullable=False,
        server_default=sa.false()))

    op.create_table(
        'worker_heartbeats',
        sa.Column('worker_id', sa.String(length=64), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('hostname', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('worker_id'),
    )
    op.create_index(
        'ix_worker_heartbeats_last_seen', 'worker_heartbeats', ['last_seen'])


def downgrade():
    op.drop_index('ix_worker_heartbeats_last_seen',
                  table_name='worker_heartbeats')
    op.drop_table('worker_heartbeats')
    op.drop_column('user', 'can_prioritise_analyses')

# vim: ts=4 sw=4 et
