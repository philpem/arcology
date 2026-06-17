"""Add HashDB recognition status and maintenance analysis types

Revision ID: 00006a302000
Revises: 00006a3194e4
Create Date: 2026-06-17 12:00:00 UTC
"""

import json
import uuid
from datetime import datetime
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
    op.execute(sa.text("""
        DELETE FROM recognised_products
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM recognised_products
            GROUP BY partition_id, product_id, folder_path
        )
    """))
    op.create_index(
        'uq_recognised_products_partition_product_folder',
        'recognised_products',
        ['partition_id', 'product_id', 'folder_path'],
        unique=True,
    )

    enabled_db_ids = [
        row[0] for row in bind.execute(sa.text(
            "SELECT id FROM hash_databases WHERE enable_product_recognition = :enabled"
        ), {'enabled': True})
    ]
    if enabled_db_ids:
        op.execute(sa.text("""
            UPDATE hash_databases
            SET product_recognition_status = 'PENDING',
                product_recognition_error = NULL
            WHERE enable_product_recognition = :enabled
        """), {'enabled': True})

        analyses = sa.table(
            'analyses',
            sa.column('uuid', sa.String),
            sa.column('artefact_id', sa.Integer),
            sa.column('analysis_type', sa.String),
            sa.column('status', sa.String),
            sa.column('hints', sa.Text),
            sa.column('created_at', sa.DateTime),
            sa.column('priority', sa.Integer),
        )
        now = datetime.utcnow()
        op.bulk_insert(analyses, [
            {
                'uuid': uuid.uuid4().hex,
                'artefact_id': None,
                'analysis_type': 'HASHDB_RECOGNITION',
                'status': 'PENDING',
                'hints': json.dumps({'database_id': db_id}, sort_keys=True),
                'created_at': now,
                'priority': 0,
            }
            for db_id in enabled_db_ids
        ])


def downgrade():
    bind = op.get_bind()
    op.execute(sa.text("""
        UPDATE artefacts
        SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses
            WHERE analysis_type IN ('HASHDB_LINK', 'HASHDB_RECOGNITION')
        )
    """))
    op.execute(sa.text(
        "DELETE FROM analyses WHERE analysis_type IN "
        "('HASHDB_LINK', 'HASHDB_RECOGNITION')"
    ))
    op.drop_column('hash_databases', 'product_recognition_error')
    op.drop_column('hash_databases', 'product_recognition_updated_at')
    op.drop_column('hash_databases', 'product_recognition_status')
    op.drop_index(
        'uq_recognised_products_partition_product_folder',
        table_name='recognised_products',
    )

    if bind.dialect.name == 'postgresql':
        sa.Enum(*_RECOGNITION_STATUS, name='productrecognitionstatus').drop(
            bind, checkfirst=True
        )

# vim: ts=4 sw=4 et
