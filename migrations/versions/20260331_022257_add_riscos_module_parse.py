"""Add RISCOS_MODULE_PARSE analysis type and riscos_modules table

Revision ID: 000069cb3001
Revises: 000069caa371
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = '000069cb3001'
down_revision = '000069caa371'
branch_labels = None
depends_on = None

# ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
autocommit = True


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'RISCOS_MODULE_PARSE'"))

    op.create_table(
        'riscos_modules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('artefact_id', sa.Integer(), sa.ForeignKey('artefacts.id'), nullable=False),
        sa.Column('title_string', sa.String(255), nullable=False),
        sa.Column('help_title', sa.String(255), nullable=True),
        sa.Column('version', sa.String(20), nullable=True),
        sa.Column('date', sa.String(10), nullable=True),
        sa.Column('swi_chunk', sa.Integer(), nullable=True),
        sa.Column('file_path', sa.String(1000), nullable=True),
        sa.Column('module_hash', sa.String(64), nullable=True),
    )
    op.create_index('ix_riscos_modules_artefact_id', 'riscos_modules', ['artefact_id'])
    op.create_index('ix_riscos_modules_title_string', 'riscos_modules', ['title_string'])


def downgrade():
    op.drop_table('riscos_modules')
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses WHERE analysis_type = 'RISCOS_MODULE_PARSE'
        )
    """))
    op.execute(sa.text("DELETE FROM analyses WHERE analysis_type = 'RISCOS_MODULE_PARSE'"))

# vim: ts=4 sw=4 et
