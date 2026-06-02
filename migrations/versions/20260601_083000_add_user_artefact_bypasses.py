"""Add user_artefact_bypasses table for per-artefact restriction bypass grants

Revision ID: 00006a1d3ed8
Revises: 00006a1d395f
Create Date: 2026-06-01 08:30:00

"""
import sqlalchemy as sa
from alembic import op

revision = '00006a1d3ed8'
down_revision = '00006a1d395f'
branch_labels = None
depends_on = None

# Restriction type enum values, matching the native `restrictiontype` PG enum
# created in 000069c2d776 (add_download_restrictions).
_RESTRICTION_VALUES = ('MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD',
                       'EXPLICIT', 'CORRUPTED')


def upgrade():
    bind = op.get_bind()

    # Column type: reference the existing native enum on PostgreSQL (the type
    # was created by an earlier migration, so create_type=False), plain VARCHAR
    # on SQLite. This keeps the column consistent with user_restriction_bypasses.
    if bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects import postgresql
        restriction_col_type = postgresql.ENUM(*_RESTRICTION_VALUES, name='restrictiontype',
                                               create_type=False)
    else:
        restriction_col_type = sa.String(50)

    op.create_table(
        'user_artefact_bypasses',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer,
                  sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('artefact_id', sa.Integer,
                  sa.ForeignKey('artefacts.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('restriction_type', restriction_col_type, nullable=False),
        sa.Column('reason', sa.Text, nullable=True),
        sa.Column('granted_by_id', sa.Integer,
                  sa.ForeignKey('user.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=True),
        sa.UniqueConstraint('user_id', 'artefact_id', 'restriction_type',
                            name='uq_user_artefact_bypass'),
    )


def downgrade():
    op.drop_table('user_artefact_bypasses')
