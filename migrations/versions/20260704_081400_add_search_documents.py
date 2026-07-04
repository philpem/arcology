"""Add search_documents for full-text document search

Stores the converted text of Acorn text files (from FORMAT_CONVERT) so the
``content:`` search key can match inside documents.  The table is created on all
backends; the PostgreSQL ``search_vector`` generated tsvector column (+ GIN
index) that powers the actual FTS is added only on PostgreSQL — SQLite falls
back to ILIKE on ``content``.

Revision ID: 00006a48c0c8
Revises: 00006a48bc25
Create Date: 2026-07-04

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a48c0c8'
down_revision = '00006a48bc25'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'search_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('artefact_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=1000), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(['artefact_id'], ['artefacts.id'], ondelete='CASCADE'),
    )
    op.create_index(
        'ix_search_documents_artefact_path', 'search_documents',
        ['artefact_id', 'file_path'])

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(
            "ALTER TABLE search_documents ADD COLUMN search_vector tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
        ))
        op.execute(sa.text(
            "CREATE INDEX ix_search_documents_search_vector "
            "ON search_documents USING GIN (search_vector)"
        ))


def downgrade():
    # Dropping the table takes its indexes and the generated column with it.
    op.drop_index('ix_search_documents_artefact_path', table_name='search_documents')
    op.drop_table('search_documents')
