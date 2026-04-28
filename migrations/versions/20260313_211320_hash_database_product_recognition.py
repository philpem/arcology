"""Add hash database product recognition and known file management

Adds:
- PRODUCT_RECOGNITION value to analysistype enum
- known_products table (product/application grouping within a hash database)
- recognised_products table (analysis results linking partitions to products)
- HashDatabase.enable_product_recognition column
- HashDatabase.is_active column
- KnownFile.product_id, is_required, relative_path columns

Revision ID: 000069b47df0
Revises: 000069b0e773
Create Date: 2026-03-13

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '000069b47df0'
down_revision = '000069b0e773'
branch_labels = None
depends_on = None

# ALTER TYPE ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()

    # Add PRODUCT_RECOGNITION to the analysistype enum.
    # Must run outside a transaction (autocommit = True above).
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'PRODUCT_RECOGNITION'"
        ))

    # Use an inspector to skip objects that already exist.  Databases that were
    # running this branch before the incremental migrations were consolidated
    # already have some or all of this schema; the bridge migration
    # (000069b45216) is a no-op and lets us reach this point safely.
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # New table: known_products
    if 'known_products' not in existing_tables:
        op.create_table(
            'known_products',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('database_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('path_match_enabled', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['database_id'], ['hash_databases.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_known_products_database_id', 'known_products', ['database_id'])
        op.create_index('ix_known_products_title', 'known_products', ['title'])

    # New table: recognised_products
    if 'recognised_products' not in existing_tables:
        op.create_table(
            'recognised_products',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('partition_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('folder_path', sa.String(1000), nullable=False),
            sa.Column('required_matched', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('required_total', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('optional_matched', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('optional_total', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['partition_id'], ['partitions.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['product_id'], ['known_products.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_recognised_products_partition_id', 'recognised_products', ['partition_id'])
        op.create_index('ix_recognised_products_product_id', 'recognised_products', ['product_id'])
        op.create_index(
            'ix_recognised_products_partition_product',
            'recognised_products',
            ['partition_id', 'product_id'],
        )

    # Extend hash_databases — add columns only if absent.
    hdb_cols = {c['name'] for c in inspector.get_columns('hash_databases')}
    if 'enable_product_recognition' not in hdb_cols:
        op.add_column('hash_databases',
            sa.Column('enable_product_recognition', sa.Boolean(), nullable=False, server_default='false'))
    if 'is_active' not in hdb_cols:
        op.add_column('hash_databases',
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'))

    # Extend known_files — add columns only if absent.
    kf_cols = {c['name'] for c in inspector.get_columns('known_files')}
    if 'product_id' not in kf_cols:
        op.add_column('known_files',
            sa.Column('product_id', sa.Integer(), nullable=True))
    if 'is_required' not in kf_cols:
        op.add_column('known_files',
            sa.Column('is_required', sa.Boolean(), nullable=False, server_default='true'))
    if 'relative_path' not in kf_cols:
        op.add_column('known_files',
            sa.Column('relative_path', sa.String(1000), nullable=True))

    kf_indexes = {i['name'] for i in inspector.get_indexes('known_files')}
    if 'ix_known_files_product_id' not in kf_indexes:
        op.create_index('ix_known_files_product_id', 'known_files', ['product_id'])

    if bind.dialect.name == 'postgresql':
        kf_fks = {fk['name'] for fk in inspector.get_foreign_keys('known_files')}
        if 'fk_known_files_product_id' not in kf_fks:
            op.create_foreign_key(
                'fk_known_files_product_id',
                'known_files', 'known_products',
                ['product_id'], ['id'],
                ondelete='SET NULL',
            )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.drop_constraint('fk_known_files_product_id', 'known_files', type_='foreignkey')

    op.drop_index('ix_known_files_product_id', 'known_files')
    op.drop_column('known_files', 'relative_path')
    op.drop_column('known_files', 'is_required')
    op.drop_column('known_files', 'product_id')

    op.drop_column('hash_databases', 'is_active')
    op.drop_column('hash_databases', 'enable_product_recognition')

    op.drop_index('ix_recognised_products_partition_product', 'recognised_products')
    op.drop_index('ix_recognised_products_product_id', 'recognised_products')
    op.drop_index('ix_recognised_products_partition_id', 'recognised_products')
    op.drop_table('recognised_products')

    op.drop_index('ix_known_products_title', 'known_products')
    op.drop_index('ix_known_products_database_id', 'known_products')
    op.drop_table('known_products')

    # Note: PostgreSQL does not support removing enum values.
    # The 'PRODUCT_RECOGNITION' value added to analysistype cannot be undone.

# vim: ts=4 sw=4 et
