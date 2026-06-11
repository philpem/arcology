"""Initial consolidated migration

Squashes the full schema history (revisions 000069a1137d .. 00006a21fc7c)
into a single root migration.  The resulting schema is identical to applying
the original 47-migration chain in sequence.

Subsequent migrations (00006a2a2054 add CLEANUP analysis type, 00006a25a2c0
global blob deduplication) chain on top of this revision unchanged.

PostgreSQL enum types are created explicitly up-front (CREATE TYPE ... AS ENUM)
so that sa.Enum(..., create_type=False) columns reference an existing type.
On SQLite the enum types are ignored and columns become VARCHAR with a CHECK.

Revision ID: 00006a21fc7c
Revises:
Create Date: 2026-06-04 22:30:20.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a21fc7c'
down_revision = None
branch_labels = None
depends_on = None

# Enum value additions later in the chain require non-transactional DDL; this
# root migration creates types wholesale, but we keep autocommit for parity with
# the rest of the chain and to allow CREATE TYPE outside an explicit transaction.
autocommit = True


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        from sqlalchemy.dialects.postgresql import ENUM as _pgENUM

        def _e(type_name, *values):
            # postgresql.ENUM(create_type=False) suppresses the before_create event
            # that would otherwise re-issue CREATE TYPE after our manual pre-creation.
            # Generic sa.Enum silently ignores create_type, so the PG-specific class
            # is required here.
            return _pgENUM(*values, name=type_name, create_type=False)

        # Create all PostgreSQL enum types before the tables that reference them.
        op.execute(sa.text("CREATE TYPE userpermission AS ENUM ('READ_ONLY', 'READ_WRITE', 'STAFF')"))
        op.execute(sa.text("CREATE TYPE apikeypermission AS ENUM ('READ_ONLY', 'READ_UPLOAD', 'READ_WRITE')"))
        op.execute(sa.text("CREATE TYPE analysisstatus AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')"))
        op.execute(sa.text("CREATE TYPE filesystemtype AS ENUM ('FAT12', 'FAT16', 'FAT32', 'NTFS', 'HPFS', 'HFS', 'HFS_PLUS', 'ADFS', 'DFS', 'AMIGA_OFS', 'AMIGA_FFS', 'ISO9660', 'CDFS', 'CPM', 'ARCHIVE', 'UNKNOWN', 'OTHER')"))
        op.execute(sa.text("CREATE TYPE storagedirectory AS ENUM ('UPLOADS', 'OUTPUTS')"))
        op.execute(sa.text("CREATE TYPE restrictiontype AS ENUM ('MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED')"))
        op.execute(sa.text("CREATE TYPE artefacttype AS ENUM ('SCP', 'DFI', 'A2R', 'IMD', 'HFE', 'RAW_SECTOR', 'ISO', 'DD_ZST', 'DD_GZ', 'DD_BZ2', 'PDF', 'ZIP', 'TARGZ', 'RAR', 'ARC', 'TBAFS', 'XFILES', 'ACORN_SPRITE', 'ACORN_DRAW', 'ACORN_TEXT', 'IMAGE', 'SIDECAR', 'UNKNOWN')"))
        op.execute(sa.text("CREATE TYPE analysistype AS ENUM ('FLUX_VISUALISATION', 'FLUX_DECODE', 'DETECT_TRACK_DENSITY', 'SECTOR_DUMP', 'FILE_EXTRACTION', 'ARCHIVE_DETECT', 'ARCHIVE_EXTRACT', 'METADATA_EXTRACT', 'PARTITION_DETECT', 'CHECKSUM_COMPUTE', 'FORMAT_IDENTIFY', 'DISC_MASTERING_DETECT', 'DISC_PROTECTION_DETECT', 'ARMLOCK_REMOVE', 'PRODUCT_RECOGNITION', 'FORMAT_CONVERT', 'RISCOS_MODULE_PARSE', 'HASH_RESCAN')"))
        op.execute(sa.text("CREATE TYPE hashrescanstatus AS ENUM ('RUNNING', 'COMPLETED', 'FAILED')"))
    else:
        def _e(type_name, *values):
            return sa.Enum(*values, name=type_name)

    op.create_table('user',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=50), nullable=False),
        sa.Column('password_hash', sa.String(length=72), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('permission', _e('userpermission', 'READ_ONLY', 'READ_WRITE', 'STAFF'), nullable=False, server_default=sa.text("'READ_WRITE'")),
        sa.Column('can_use_api', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('preferences', sa.JSON(), nullable=True),
        sa.Column('oidc_sub', sa.String(length=255), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('oidc_managed', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
    )
    op.create_unique_constraint('uq_user_oidc_sub', 'user', ['oidc_sub'])
    op.create_index('ix_user_oidc_sub', 'user', ['oidc_sub'], unique=False)
    op.create_table('categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['parent_id'], ['categories.id']),
    )
    op.create_index('ix_categories_name', 'categories', ['name'], unique=True)
    op.create_table('platforms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['parent_id'], ['platforms.id']),
    )
    op.create_index('ix_platforms_name', 'platforms', ['name'], unique=True)
    op.create_table('tags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tags_name', 'tags', ['name'], unique=True)
    op.create_table('external_systems',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('system_type', sa.String(length=50), nullable=True),
        sa.Column('base_url', sa.String(length=500), nullable=True),
        sa.Column('url_template', sa.String(length=200), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_external_systems_name', 'external_systems', ['name'], unique=True)
    op.create_table('groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False, server_default=sa.text("'local'")),
        sa.Column('oidc_claim_name', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_groups_name'),
    )
    op.create_index('ix_groups_name', 'groups', ['name'], unique=False)
    op.create_index('ix_groups_oidc_claim_name', 'groups', ['oidc_claim_name'], unique=False)
    op.create_table('hash_databases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source_url', sa.String(length=500), nullable=True),
        sa.Column('version', sa.String(length=50), nullable=True),
        sa.Column('platform_id', sa.Integer(), nullable=True),
        sa.Column('file_count', sa.Integer(), nullable=True),
        sa.Column('enable_product_recognition', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('restriction_type', _e('restrictiontype', 'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['platform_id'], ['platforms.id'], ondelete='SET NULL', name='fk_hash_databases_platform_id'),
    )
    op.create_index('ix_hash_databases_name', 'hash_databases', ['name'], unique=True)
    op.create_table('known_products',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('database_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('path_match_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['database_id'], ['hash_databases.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_known_products_database_id', 'known_products', ['database_id'], unique=False)
    op.create_index('ix_known_products_title', 'known_products', ['title'], unique=False)
    op.create_table('known_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('database_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('md5', sa.String(length=32), nullable=True),
        sa.Column('sha1', sa.String(length=40), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('crc32', sa.String(length=8), nullable=True),
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('relative_path', sa.String(length=1000), nullable=True),
        sa.Column('product_name', sa.String(length=200), nullable=True),
        sa.Column('product_version', sa.String(length=50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['database_id'], ['hash_databases.id']),
        sa.ForeignKeyConstraint(['product_id'], ['known_products.id'], ondelete='SET NULL', name='fk_known_files_product_id'),
    )
    op.create_index('ix_known_files_database_id', 'known_files', ['database_id'], unique=False)
    op.create_index('ix_known_files_filename', 'known_files', ['filename'], unique=False)
    op.create_index('ix_known_files_md5', 'known_files', ['md5'], unique=False)
    op.create_index('ix_known_files_md5_size', 'known_files', ['md5', 'file_size'], unique=False)
    op.create_index('ix_known_files_product_id', 'known_files', ['product_id'], unique=False)
    op.create_index('ix_known_files_sha1', 'known_files', ['sha1'], unique=False)
    op.create_index('ix_known_files_sha1_size', 'known_files', ['sha1', 'file_size'], unique=False)
    op.create_table('items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('platform_id', sa.Integer(), nullable=True),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('private_effective', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['platform_id'], ['platforms.id']),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['items.id'], name='fk_items_parent_id'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='SET NULL', name='fk_items_owner_id_user'),
    )
    op.create_index('ix_items_category_id', 'items', ['category_id'], unique=False)
    op.create_index('ix_items_name', 'items', ['name'], unique=False)
    op.create_index('ix_items_owner_id', 'items', ['owner_id'], unique=False)
    op.create_index('ix_items_parent_id', 'items', ['parent_id'], unique=False)
    op.create_index('ix_items_platform_id', 'items', ['platform_id'], unique=False)
    op.create_index('ix_items_private_effective', 'items', ['private_effective'], unique=False)
    op.create_index('ix_items_slug', 'items', ['slug'], unique=False)
    op.create_index('ix_items_uuid', 'items', ['uuid'], unique=True)
    op.create_table('item_tags',
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('item_id', 'tag_id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id']),
    )
    op.create_table('external_references',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('system_id', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(length=200), nullable=False),
        sa.Column('external_url', sa.String(length=500), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['system_id'], ['external_systems.id']),
    )
    op.create_index('ix_external_references_external_id', 'external_references', ['external_id'], unique=False)
    op.create_index('ix_external_references_item_id', 'external_references', ['item_id'], unique=False)
    op.create_index('ix_external_references_system_external', 'external_references', ['system_id', 'external_id'], unique=False)
    op.create_index('ix_external_references_system_id', 'external_references', ['system_id'], unique=False)
    op.create_table('item_shares',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('permission', sa.String(length=20), nullable=False, server_default=sa.text("'viewer'")),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
        sa.CheckConstraint('(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)', name='ck_item_shares_exactly_one_principal'),
        sa.UniqueConstraint('item_id', 'group_id', name='uq_item_share_group'),
        sa.UniqueConstraint('item_id', 'user_id', name='uq_item_share_user'),
    )
    op.create_index('ix_item_shares_group_id', 'item_shares', ['group_id'], unique=False)
    op.create_index('ix_item_shares_item_id', 'item_shares', ['item_id'], unique=False)
    op.create_index('ix_item_shares_user_id', 'item_shares', ['user_id'], unique=False)
    op.create_table('group_memberships',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'group_id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
    )
    op.create_table('api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('key_prefix', sa.String(length=8), nullable=False),
        sa.Column('key_hash', sa.String(length=72), nullable=False),
        sa.Column('permission', _e('apikeypermission', 'READ_ONLY', 'READ_UPLOAD', 'READ_WRITE'), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.UniqueConstraint('key_hash'),
    )
    op.create_index('ix_api_keys_key_hash', 'api_keys', ['key_hash'], unique=True)
    op.create_index('ix_api_keys_prefix_active', 'api_keys', ['key_prefix', 'is_active'], unique=False)
    op.create_index('ix_api_keys_user_id', 'api_keys', ['user_id'], unique=False)
    op.create_table('user_restriction_bypasses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type', _e('restrictiontype', 'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.UniqueConstraint('user_id', 'restriction_type', name='uq_user_restriction_bypass'),
    )
    op.create_index('ix_user_restriction_bypasses_user_id', 'user_restriction_bypasses', ['user_id'], unique=False)
    op.create_table('artefacts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('label', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=True),
        sa.Column('artefact_type', _e('artefacttype', 'SCP', 'DFI', 'A2R', 'IMD', 'HFE', 'RAW_SECTOR', 'ISO', 'DD_ZST', 'DD_GZ', 'DD_BZ2', 'PDF', 'ZIP', 'TARGZ', 'RAR', 'ARC', 'TBAFS', 'XFILES', 'ACORN_SPRITE', 'ACORN_DRAW', 'ACORN_TEXT', 'IMAGE', 'SIDECAR', 'UNKNOWN'), nullable=False),
        sa.Column('type_overridden', sa.Boolean(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('storage_path', sa.String(length=1000), nullable=False),
        sa.Column('storage_directory', _e('storagedirectory', 'UPLOADS', 'OUTPUTS'), nullable=False),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('mime_type', sa.String(length=100), nullable=True),
        sa.Column('md5', sa.String(length=32), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('media_metadata', sa.Text(), nullable=True),
        sa.Column('parent_artefact_id', sa.Integer(), nullable=True),
        sa.Column('derived_from_analysis_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='SET NULL', name='fk_artefacts_owner_id_user'),
        sa.ForeignKeyConstraint(['parent_artefact_id'], ['artefacts.id']),
        sa.UniqueConstraint('derived_from_analysis_id', 'storage_path', name='uq_artefact_analysis_storage_path'),
        sa.UniqueConstraint('item_id', 'sha256', name='uq_artefact_item_sha256'),
    )
    op.create_index('ix_artefacts_derived_from_analysis_id', 'artefacts', ['derived_from_analysis_id'], unique=False)
    op.create_index('ix_artefacts_item_id', 'artefacts', ['item_id'], unique=False)
    op.create_index('ix_artefacts_owner_id', 'artefacts', ['owner_id'], unique=False)
    op.create_index('ix_artefacts_parent_artefact_id', 'artefacts', ['parent_artefact_id'], unique=False)
    op.create_index('ix_artefacts_slug', 'artefacts', ['slug'], unique=False)
    op.create_index('ix_artefacts_uuid', 'artefacts', ['uuid'], unique=True)
    op.create_table('analyses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('analysis_type', _e('analysistype', 'FLUX_VISUALISATION', 'FLUX_DECODE', 'DETECT_TRACK_DENSITY', 'SECTOR_DUMP', 'FILE_EXTRACTION', 'ARCHIVE_DETECT', 'ARCHIVE_EXTRACT', 'METADATA_EXTRACT', 'PARTITION_DETECT', 'CHECKSUM_COMPUTE', 'FORMAT_IDENTIFY', 'DISC_MASTERING_DETECT', 'DISC_PROTECTION_DETECT', 'ARMLOCK_REMOVE', 'PRODUCT_RECOGNITION', 'FORMAT_CONVERT', 'RISCOS_MODULE_PARSE', 'HASH_RESCAN'), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=True),
        sa.Column('status', _e('analysisstatus', 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED'), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=True),
        sa.Column('tool_version', sa.String(length=50), nullable=True),
        sa.Column('hints', sa.Text(), nullable=True),
        sa.Column('output_url', sa.String(length=500), nullable=True),
        sa.Column('output_path', sa.String(length=1000), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
    )
    op.create_index('ix_analyses_artefact_id', 'analyses', ['artefact_id'], unique=False)
    op.create_index('ix_analyses_created_at', 'analyses', ['created_at'], unique=False)
    op.create_index('ix_analyses_priority', 'analyses', ['priority'], unique=False)
    op.create_index('ix_analyses_slug', 'analyses', ['slug'], unique=False)
    op.create_index('ix_analyses_status', 'analyses', ['status'], unique=False)
    op.create_index('ix_analyses_status_created', 'analyses', ['status', 'created_at'], unique=False)
    op.create_index('ix_analyses_status_priority_created', 'analyses', ['status', 'priority', 'created_at'], unique=False)
    op.create_index('ix_analyses_uuid', 'analyses', ['uuid'], unique=True)
    # Deferred circular FK: artefacts.derived_from_analysis_id -> analyses.id.
    # SQLite cannot ALTER TABLE to add a constraint; only needed on PostgreSQL.
    if is_pg:
        op.create_foreign_key('fk_artefacts_derived_from_analysis', 'artefacts', 'analyses', ['derived_from_analysis_id'], ['id'], ondelete='SET NULL')
    op.create_table('artefact_tags',
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('artefact_id', 'tag_id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id']),
    )
    op.create_table('artefact_mastering',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('mastering_type', sa.String(length=64), nullable=False),
        sa.Column('track', sa.Integer(), nullable=True),
        sa.Column('decoded', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
    )
    op.create_index('ix_artefact_mastering_artefact_id', 'artefact_mastering', ['artefact_id'], unique=False)
    op.create_index('ix_artefact_mastering_mastering_type', 'artefact_mastering', ['mastering_type'], unique=False)
    op.create_table('artefact_protection',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('protection_type', sa.String(length=64), nullable=False),
        sa.Column('track', sa.Integer(), nullable=True),
        sa.Column('side', sa.Integer(), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
    )
    op.create_index('ix_artefact_protection_artefact_id', 'artefact_protection', ['artefact_id'], unique=False)
    op.create_index('ix_artefact_protection_protection_type', 'artefact_protection', ['protection_type'], unique=False)
    op.create_table('artefact_restrictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type', _e('restrictiontype', 'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('added_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
        sa.ForeignKeyConstraint(['added_by_id'], ['user.id']),
        sa.UniqueConstraint('artefact_id', 'restriction_type', name='uq_artefact_restriction_type'),
    )
    op.create_index('ix_artefact_restrictions_artefact_id', 'artefact_restrictions', ['artefact_id'], unique=False)
    op.create_table('riscos_modules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('title_string', sa.String(length=255), nullable=False),
        sa.Column('help_title', sa.String(length=255), nullable=True),
        sa.Column('version', sa.String(length=20), nullable=True),
        sa.Column('date', sa.String(length=10), nullable=True),
        sa.Column('swi_chunk', sa.Integer(), nullable=True),
        sa.Column('file_path', sa.String(length=1000), nullable=True),
        sa.Column('module_hash', sa.String(length=64), nullable=True),
        sa.Column('commands', sa.Text(), nullable=True),
        sa.Column('swi_names', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
    )
    op.create_index('ix_riscos_modules_artefact_id', 'riscos_modules', ['artefact_id'], unique=False)
    op.create_index('ix_riscos_modules_title_string', 'riscos_modules', ['title_string'], unique=False)
    op.create_table('user_artefact_bypasses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type', _e('restrictiontype', 'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('granted_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by_id'], ['user.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('user_id', 'artefact_id', 'restriction_type', name='uq_user_artefact_bypass'),
    )
    op.create_index('ix_user_artefact_bypasses_artefact_id', 'user_artefact_bypasses', ['artefact_id'], unique=False)
    op.create_index('ix_user_artefact_bypasses_user_id', 'user_artefact_bypasses', ['user_id'], unique=False)
    op.create_table('partitions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('partition_index', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=True),
        sa.Column('slug', sa.String(length=255), nullable=True),
        sa.Column('filesystem', _e('filesystemtype', 'FAT12', 'FAT16', 'FAT32', 'NTFS', 'HPFS', 'HFS', 'HFS_PLUS', 'ADFS', 'DFS', 'AMIGA_OFS', 'AMIGA_FFS', 'ISO9660', 'CDFS', 'CPM', 'ARCHIVE', 'UNKNOWN', 'OTHER'), nullable=False),
        sa.Column('container_format', sa.Text(), nullable=True),
        sa.Column('archive_comment', sa.Text(), nullable=True),
        sa.Column('start_sector', sa.BigInteger(), nullable=True),
        sa.Column('sector_count', sa.BigInteger(), nullable=True),
        sa.Column('block_size', sa.Integer(), nullable=True),
        sa.Column('total_files', sa.Integer(), nullable=True),
        sa.Column('total_directories', sa.Integer(), nullable=True),
        sa.Column('total_bytes', sa.BigInteger(), nullable=True),
        sa.Column('unique_files', sa.Integer(), nullable=True),
        sa.Column('detection_details', sa.Text(), nullable=True),
        sa.Column('gnu_file_type', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id']),
        sa.UniqueConstraint('artefact_id', 'partition_index', name='uq_partition_artefact_index'),
    )
    op.create_index('ix_partitions_artefact_id', 'partitions', ['artefact_id'], unique=False)
    op.create_index('ix_partitions_gnu_file_type', 'partitions', ['gnu_file_type'], unique=False)
    op.create_index('ix_partitions_slug', 'partitions', ['slug'], unique=False)
    op.create_index('ix_partitions_uuid', 'partitions', ['uuid'], unique=True)
    op.create_table('recognised_products',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('partition_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('folder_path', sa.String(length=1000), nullable=False),
        sa.Column('required_matched', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('required_total', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('optional_matched', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('optional_total', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['partition_id'], ['partitions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['known_products.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_recognised_products_partition_id', 'recognised_products', ['partition_id'], unique=False)
    op.create_index('ix_recognised_products_partition_product', 'recognised_products', ['partition_id', 'product_id'], unique=False)
    op.create_index('ix_recognised_products_product_id', 'recognised_products', ['product_id'], unique=False)
    op.create_table('extracted_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=32), nullable=False),
        sa.Column('partition_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=1000), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('extension', sa.String(length=255), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('created_time', sa.DateTime(), nullable=True),
        sa.Column('modified_time', sa.DateTime(), nullable=True),
        sa.Column('accessed_time', sa.DateTime(), nullable=True),
        sa.Column('attributes', sa.String(length=50), nullable=True),
        sa.Column('md5', sa.String(length=32), nullable=True),
        sa.Column('sha1', sa.String(length=40), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('known_file_id', sa.Integer(), nullable=True),
        sa.Column('is_known', sa.Boolean(), nullable=False),
        sa.Column('parent_file_id', sa.Integer(), nullable=True),
        sa.Column('is_archive', sa.Boolean(), nullable=False),
        sa.Column('is_directory', sa.Boolean(), nullable=False),
        sa.Column('archive_format', sa.String(length=50), nullable=True),
        sa.Column('archive_comment', sa.Text(), nullable=True),
        sa.Column('risc_os_filetype', sa.String(length=3), nullable=True),
        sa.Column('load_address', sa.String(length=8), nullable=True),
        sa.Column('exec_address', sa.String(length=8), nullable=True),
        sa.Column('extraction_depth', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['partition_id'], ['partitions.id']),
        sa.ForeignKeyConstraint(['known_file_id'], ['known_files.id'], ondelete='SET NULL', name='fk_extracted_files_known_file_id'),
        sa.ForeignKeyConstraint(['parent_file_id'], ['extracted_files.id']),
    )
    op.create_index('ix_extracted_files_archive', 'extracted_files', ['is_archive', 'risc_os_filetype'], unique=False)
    op.create_index('ix_extracted_files_extension', 'extracted_files', ['extension'], unique=False)
    op.create_index('ix_extracted_files_filename', 'extracted_files', ['filename'], unique=False)
    op.create_index('ix_extracted_files_is_archive', 'extracted_files', ['is_archive'], unique=False)
    op.create_index('ix_extracted_files_is_directory', 'extracted_files', ['is_directory'], unique=False)
    op.create_index('ix_extracted_files_is_known', 'extracted_files', ['is_known'], unique=False)
    op.create_index('ix_extracted_files_known_file_id', 'extracted_files', ['known_file_id'], unique=False)
    op.create_index('ix_extracted_files_md5', 'extracted_files', ['md5'], unique=False)
    op.create_index('ix_extracted_files_parent', 'extracted_files', ['parent_file_id', 'extraction_depth'], unique=False)
    op.create_index('ix_extracted_files_parent_file_id', 'extracted_files', ['parent_file_id'], unique=False)
    op.create_index('ix_extracted_files_partition_id', 'extracted_files', ['partition_id'], unique=False)
    op.create_index('ix_extracted_files_partition_known', 'extracted_files', ['partition_id', 'is_known'], unique=False)
    op.create_index('ix_extracted_files_partition_path', 'extracted_files', ['partition_id', 'path'], unique=False)
    op.create_index('ix_extracted_files_risc_os_filetype', 'extracted_files', ['risc_os_filetype'], unique=False)
    op.create_index('ix_extracted_files_sha1', 'extracted_files', ['sha1'], unique=False)
    op.create_index('ix_extracted_files_uuid', 'extracted_files', ['uuid'], unique=True)
    op.create_table('extracted_file_restrictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('extracted_file_id', sa.Integer(), nullable=False),
        sa.Column('restriction_type', _e('restrictiontype', 'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD', 'EXPLICIT', 'CORRUPTED'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('added_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['extracted_file_id'], ['extracted_files.id']),
        sa.ForeignKeyConstraint(['added_by_id'], ['user.id']),
        sa.UniqueConstraint('extracted_file_id', 'restriction_type', name='uq_extracted_file_restriction_type'),
    )
    op.create_index('ix_extracted_file_restrictions_extracted_file_id', 'extracted_file_restrictions', ['extracted_file_id'], unique=False)
    op.create_table('hash_rescan_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('database_id', sa.Integer(), nullable=True),
        sa.Column('status', _e('hashrescanstatus', 'RUNNING', 'COMPLETED', 'FAILED'), nullable=False),
        sa.Column('files_updated', sa.Integer(), nullable=True),
        sa.Column('files_total', sa.Integer(), nullable=True),
        sa.Column('queued_analyses', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['database_id'], ['hash_databases.id'], ondelete='SET NULL'),
    )
    op.create_index('ix_hash_rescan_jobs_database_id', 'hash_rescan_jobs', ['database_id'], unique=False)


def downgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    # Drop the deferred circular FK first so the two tables can be removed.
    # SQLite has no ALTER-DROP-CONSTRAINT (and never created the FK above).
    if is_pg:
        op.drop_constraint('fk_artefacts_derived_from_analysis', 'artefacts', type_='foreignkey')
    op.drop_table('hash_rescan_jobs')
    op.drop_table('extracted_file_restrictions')
    op.drop_table('extracted_files')
    op.drop_table('recognised_products')
    op.drop_table('partitions')
    op.drop_table('user_artefact_bypasses')
    op.drop_table('riscos_modules')
    op.drop_table('artefact_restrictions')
    op.drop_table('artefact_protection')
    op.drop_table('artefact_mastering')
    op.drop_table('artefact_tags')
    op.drop_table('analyses')
    op.drop_table('artefacts')
    op.drop_table('user_restriction_bypasses')
    op.drop_table('api_keys')
    op.drop_table('group_memberships')
    op.drop_table('item_shares')
    op.drop_table('external_references')
    op.drop_table('item_tags')
    op.drop_table('items')
    op.drop_table('known_files')
    op.drop_table('known_products')
    op.drop_table('hash_databases')
    op.drop_table('groups')
    op.drop_table('external_systems')
    op.drop_table('tags')
    op.drop_table('platforms')
    op.drop_table('categories')
    op.drop_table('user')

    if bind.dialect.name == 'postgresql':
        op.execute(sa.text('DROP TYPE IF EXISTS hashrescanstatus'))
        op.execute(sa.text('DROP TYPE IF EXISTS analysistype'))
        op.execute(sa.text('DROP TYPE IF EXISTS artefacttype'))
        op.execute(sa.text('DROP TYPE IF EXISTS restrictiontype'))
        op.execute(sa.text('DROP TYPE IF EXISTS storagedirectory'))
        op.execute(sa.text('DROP TYPE IF EXISTS filesystemtype'))
        op.execute(sa.text('DROP TYPE IF EXISTS analysisstatus'))
        op.execute(sa.text('DROP TYPE IF EXISTS apikeypermission'))
        op.execute(sa.text('DROP TYPE IF EXISTS userpermission'))

# vim: ts=4 sw=4 et
