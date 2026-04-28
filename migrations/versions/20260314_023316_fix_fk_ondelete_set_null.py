"""Fix nullable FK columns to use ON DELETE SET NULL

Adds ON DELETE SET NULL to:
- extracted_files.known_file_id -> known_files.id
- hash_databases.platform_id -> platforms.id

These are nullable FKs, so deleting the referenced row should set the
FK column to NULL rather than raising an IntegrityError.

Revision ID: 000069b4c8ec
Revises: 000069b47df0
Create Date: 2026-03-14

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069b4c8ec'
down_revision = '000069b47df0'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    inspector = sa.inspect(bind)

    # --- extracted_files.known_file_id -> known_files.id (SET NULL) ---
    ef_fks = {fk['name']: fk for fk in inspector.get_foreign_keys('extracted_files')}
    # Drop the existing unnamed/named FK on known_file_id, then recreate with ondelete
    for fk_name, fk in ef_fks.items():
        if fk['referred_table'] == 'known_files' and 'known_file_id' in fk['constrained_columns']:
            if fk_name:
                op.drop_constraint(fk_name, 'extracted_files', type_='foreignkey')
    op.create_foreign_key(
        'fk_extracted_files_known_file_id',
        'extracted_files', 'known_files',
        ['known_file_id'], ['id'],
        ondelete='SET NULL',
    )

    # --- hash_databases.platform_id -> platforms.id (SET NULL) ---
    hdb_fks = {fk['name']: fk for fk in inspector.get_foreign_keys('hash_databases')}
    for fk_name, fk in hdb_fks.items():
        if fk['referred_table'] == 'platforms' and 'platform_id' in fk['constrained_columns']:
            if fk_name:
                op.drop_constraint(fk_name, 'hash_databases', type_='foreignkey')
    op.create_foreign_key(
        'fk_hash_databases_platform_id',
        'hash_databases', 'platforms',
        ['platform_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    op.drop_constraint('fk_extracted_files_known_file_id', 'extracted_files', type_='foreignkey')
    op.create_foreign_key(
        None,
        'extracted_files', 'known_files',
        ['known_file_id'], ['id'],
    )

    op.drop_constraint('fk_hash_databases_platform_id', 'hash_databases', type_='foreignkey')
    op.create_foreign_key(
        None,
        'hash_databases', 'platforms',
        ['platform_id'], ['id'],
    )

# vim: ts=4 sw=4 et
