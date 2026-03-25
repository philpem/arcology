"""Add indexes for query performance under concurrent worker load

Covers: Analysis status/created_at filtering and ordering, composite index
for worker job claiming, ApiKey prefix lookups on every authenticated request,
and ExtractedFile duplicate detection during bulk file uploads.

Revision ID: 000069c2d753
Revises: 000069b4c8ec
Create Date: 2026-03-24

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '000069c2d753'
down_revision = '000069c2f0db'
branch_labels = None
depends_on = None


def upgrade():
    # Analysis: status filtering (dashboard, analysis views, worker polling)
    op.create_index('ix_analyses_status', 'analyses', ['status'])
    # Analysis: created_at ordering (dashboard, analysis list)
    op.create_index('ix_analyses_created_at', 'analyses', ['created_at'])
    # Analysis: composite for worker polling (WHERE status=PENDING ORDER BY created_at)
    op.create_index('ix_analyses_status_created', 'analyses', ['status', 'created_at'])
    # ApiKey: prefix+active lookup on every authenticated API request
    op.create_index('ix_api_keys_prefix_active', 'api_keys', ['key_prefix', 'is_active'])
    # ExtractedFile: duplicate detection during bulk file uploads (partition_id + path)
    op.create_index('ix_extracted_files_partition_path', 'extracted_files', ['partition_id', 'path'])


def downgrade():
    op.drop_index('ix_extracted_files_partition_path', table_name='extracted_files')
    op.drop_index('ix_api_keys_prefix_active', table_name='api_keys')
    op.drop_index('ix_analyses_status_created', table_name='analyses')
    op.drop_index('ix_analyses_created_at', table_name='analyses')
    op.drop_index('ix_analyses_status', table_name='analyses')


# vim: ts=4 sw=4 et
