"""Add KnownProduct, RecognisedProduct tables; extend HashDatabase and KnownFile

Adds:
- known_products table (product/application grouping within a hash database)
- recognised_products table (analysis results linking partitions to products)
- HashDatabase.enable_product_recognition column
- KnownFile.product_id, is_required, relative_path columns

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-03-12 00:00:02.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    # New table: known_products
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

    # Extend hash_databases
    op.add_column('hash_databases',
        sa.Column('enable_product_recognition', sa.Boolean(), nullable=False, server_default='false'))

    # Extend known_files
    op.add_column('known_files',
        sa.Column('product_id', sa.Integer(), nullable=True))
    op.add_column('known_files',
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('known_files',
        sa.Column('relative_path', sa.String(1000), nullable=True))

    op.create_index('ix_known_files_product_id', 'known_files', ['product_id'])

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.create_foreign_key(
            'fk_known_files_product_id',
            'known_files', 'known_products',
            ['product_id'], ['id'],
            ondelete='SET NULL',
        )
    else:
        # SQLite: skip named FK constraints (not enforced anyway)
        pass


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.drop_constraint('fk_known_files_product_id', 'known_files', type_='foreignkey')

    op.drop_index('ix_known_files_product_id', 'known_files')
    op.drop_column('known_files', 'relative_path')
    op.drop_column('known_files', 'is_required')
    op.drop_column('known_files', 'product_id')

    op.drop_column('hash_databases', 'enable_product_recognition')

    op.drop_index('ix_recognised_products_partition_product', 'recognised_products')
    op.drop_index('ix_recognised_products_product_id', 'recognised_products')
    op.drop_index('ix_recognised_products_partition_id', 'recognised_products')
    op.drop_table('recognised_products')

    op.drop_index('ix_known_products_title', 'known_products')
    op.drop_index('ix_known_products_database_id', 'known_products')
    op.drop_table('known_products')

# vim: ts=4 sw=4 et
