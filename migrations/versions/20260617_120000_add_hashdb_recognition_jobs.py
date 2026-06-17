"""Add HashDB recognition status and maintenance analysis types

Revision ID: 00006a302000
Revises: 00006a3194e4
Create Date: 2026-06-17 12:00:00 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a302000"
down_revision = "00006a3194e4"
branch_labels = None
depends_on = None


_RECOGNITION_STATUS = ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'HASHDB_LINK'"
            ))
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'HASHDB_RECOGNITION'"
            ))
        status_type = sa.Enum(*_RECOGNITION_STATUS, name='productrecognitionstatus')
        status_type.create(bind, checkfirst=True)
    else:
        status_type = sa.Enum(*_RECOGNITION_STATUS, name='productrecognitionstatus')

    op.add_column(
        'hash_databases',
        sa.Column('product_recognition_status', status_type, nullable=True),
    )
    op.add_column(
        'hash_databases',
        sa.Column('product_recognition_updated_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'hash_databases',
        sa.Column('product_recognition_error', sa.Text(), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    op.drop_column('hash_databases', 'product_recognition_error')
    op.drop_column('hash_databases', 'product_recognition_updated_at')
    op.drop_column('hash_databases', 'product_recognition_status')

    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "DELETE FROM analyses WHERE analysis_type IN "
            "('HASHDB_LINK', 'HASHDB_RECOGNITION')"
        ))
        sa.Enum(*_RECOGNITION_STATUS, name='productrecognitionstatus').drop(
            bind, checkfirst=True
        )

# vim: ts=4 sw=4 et
