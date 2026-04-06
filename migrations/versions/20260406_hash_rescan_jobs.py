"""Add hash_rescan_jobs table

Tracks background hash-rescan operations triggered from the Hash DB UI.
Status is stored in the database so all gunicorn workers share the same view.

Revision ID: 000069d43024
Revises: 000069d2fd9e
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = '000069d43024'
down_revision = '000069d2fd9e'
branch_labels = None
depends_on = None

_STATUS_VALUES = ('RUNNING', 'COMPLETED', 'FAILED')


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # On PostgreSQL, create the native enum type first via raw SQL.
    # Using sa.Enum() for the column would cause SQLAlchemy to try CREATE TYPE
    # again inside the transaction, failing with "type already exists".
    if is_pg:
        vals = ', '.join(f"'{v}'" for v in _STATUS_VALUES)
        op.execute(sa.text(
            f"DO $$ BEGIN "
            f"  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hashrescanstatus') THEN "
            f"    CREATE TYPE hashrescanstatus AS ENUM ({vals}); "
            f"  END IF; "
            f"END $$"
        ))

    # Column type: native enum on PostgreSQL, plain VARCHAR on SQLite
    if is_pg:
        from sqlalchemy.dialects import postgresql
        status_col_type = postgresql.ENUM(*_STATUS_VALUES, name='hashrescanstatus',
                                          create_type=False)
    else:
        status_col_type = sa.String(20)

    op.create_table(
        'hash_rescan_jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('database_id', sa.Integer(),
                  sa.ForeignKey('hash_databases.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('status', status_col_type, nullable=False),
        sa.Column('files_updated', sa.Integer(), nullable=True),
        sa.Column('files_total', sa.Integer(), nullable=True),
        sa.Column('queued_analyses', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('hash_rescan_jobs')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("DROP TYPE IF EXISTS hashrescanstatus"))

# vim: ts=4 sw=4 et
