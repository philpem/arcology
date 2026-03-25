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

_RESTRICTION_VALUES = ('MALWARE', 'PII', 'COPYRIGHT', 'LEGAL_HOLD',
                       'EXPLICIT', 'CORRUPTED')


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # On PostgreSQL, create the native enum type first via raw SQL.
    # We deliberately avoid sa.Enum() for column types in this migration
    # because SQLAlchemy's _on_table_create hook ignores create_type=False
    # on sa.Enum (that flag only works on postgresql.ENUM) and would try
    # to CREATE TYPE again, failing with "type already exists".
    if is_pg:
        vals = ', '.join(f"'{v}'" for v in _RESTRICTION_VALUES)
        op.execute(sa.text(
            f"DO $$ BEGIN "
            f"  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'restrictiontype') THEN "
            f"    CREATE TYPE restrictiontype AS ENUM ({vals}); "
            f"  END IF; "
            f"END $$"
        ))

    # Column type: native enum on PostgreSQL, plain VARCHAR on SQLite
    if is_pg:
        # Reference the already-created PG enum by its type name.
        # Using sa.text() as the type tells Alembic to emit the type name
        # literally without trying to manage the enum lifecycle.
        from sqlalchemy.dialects import postgresql
        col_type = postgresql.ENUM(*_RESTRICTION_VALUES, name='restrictiontype',
                                   create_type=False)
    else:
        col_type = sa.String(50)

    # Create artefact_restrictions table
    op.create_table(
        'artefact_restrictions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('artefact_id', sa.Integer(), sa.ForeignKey('artefacts.id'), nullable=False, index=True),
        sa.Column('restriction_type', col_type, nullable=False),
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
        sa.Column('restriction_type', col_type, nullable=False),
        sa.UniqueConstraint('user_id', 'restriction_type', name='uq_user_restriction_bypass'),
    )

    # Add restriction_type column to hash_databases
    op.add_column('hash_databases', sa.Column('restriction_type', col_type, nullable=True))


def downgrade():
    bind = op.get_bind()

    op.drop_column('hash_databases', 'restriction_type')
    op.drop_table('user_restriction_bypasses')
    op.drop_table('artefact_restrictions')

    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("DROP TYPE IF EXISTS restrictiontype"))

# vim: ts=4 sw=4 et
