"""Add download restriction system: artefact restrictions, user bypass permissions, hash database restriction flag

Revision ID: 000069c2d776
Revises: 000069ae3f4d
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '000069c2d776'
down_revision = '000069c2f0db'
branch_labels = None
depends_on = None


def upgrade():
    # Create restrictiontype enum for PostgreSQL; SQLite uses VARCHAR with CHECK.
    # We create the type explicitly first, then pass create_type=False so
    # SQLAlchemy does not attempt to auto-create it again inside create_table().
    restriction_enum = sa.Enum(
        'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD',
        'EXPORT_CONTROL', 'NSFW', 'CORRUPTED',
        name='restrictiontype',
    )

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        restriction_enum.create(bind, checkfirst=True)

    # After explicit creation, prevent SQLAlchemy from trying again
    col_enum = sa.Enum(
        'MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD',
        'EXPORT_CONTROL', 'NSFW', 'CORRUPTED',
        name='restrictiontype',
        create_type=False,
    )

    # Create artefact_restrictions table
    op.create_table(
        'artefact_restrictions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('artefact_id', sa.Integer(), sa.ForeignKey('artefacts.id'), nullable=False, index=True),
        sa.Column('restriction_type', col_enum, nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('added_by_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.UniqueConstraint('artefact_id', 'restriction_type', name='uq_artefact_restriction_type'),
    )

    # Create user_restriction_bypasses table
    op.create_table(
        'user_restriction_bypasses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False, index=True),
        sa.Column('restriction_type', col_enum, nullable=False),
        sa.UniqueConstraint('user_id', 'restriction_type', name='uq_user_restriction_bypass'),
    )

    # Add restriction_type column to hash_databases
    op.add_column('hash_databases', sa.Column('restriction_type', col_enum, nullable=True))


def downgrade():
    bind = op.get_bind()

    op.drop_column('hash_databases', 'restriction_type')
    op.drop_table('user_restriction_bypasses')
    op.drop_table('artefact_restrictions')

    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("DROP TYPE IF EXISTS restrictiontype"))

# vim: ts=4 sw=4 et
