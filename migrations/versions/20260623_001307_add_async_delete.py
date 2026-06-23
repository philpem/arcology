"""Add async item/artefact deletion: ITEM_DELETE & ARTEFACT_DELETE analysis
types and the pending_deletion flags.

Adds the ``ITEM_DELETE`` and ``ARTEFACT_DELETE`` values to the ``analysistype``
enum (control-plane jobs the task runner uses to batch-delete a large
item/artefact subtree off the synchronous request path) and a
``pending_deletion`` boolean to both ``items`` and ``artefacts``.  The web
request flags the target subtree pending_deletion — hiding it from every
visibility surface immediately — and the task runner then deletes the rows.

Revision ID: 00006a39cf93
Revises: 00006a3670f7
Create Date: 2026-06-23 00:13:07.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a39cf93'
down_revision = '00006a3670f7'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'ITEM_DELETE'"
            ))
            op.execute(sa.text(
                "ALTER TYPE analysistype ADD VALUE IF NOT EXISTS 'ARTEFACT_DELETE'"
            ))

    op.add_column('items', sa.Column(
        'pending_deletion', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.create_index('ix_items_pending_deletion', 'items', ['pending_deletion'])

    op.add_column('artefacts', sa.Column(
        'pending_deletion', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.create_index(
        'ix_artefacts_pending_deletion', 'artefacts', ['pending_deletion'])


def downgrade():
    op.drop_index('ix_artefacts_pending_deletion', table_name='artefacts')
    op.drop_column('artefacts', 'pending_deletion')
    op.drop_index('ix_items_pending_deletion', table_name='items')
    op.drop_column('items', 'pending_deletion')

    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # PostgreSQL cannot drop an enum value; delete rows using the new types so
    # the ORM does not crash with LookupError after a downgrade.  Null any stray
    # derived_from_analysis_id reference defensively first (these jobs never
    # produce derived artefacts, but the FK may lack ON DELETE SET NULL at this
    # point in the downgrade chain).
    op.execute(sa.text("""
        UPDATE artefacts SET derived_from_analysis_id = NULL
        WHERE derived_from_analysis_id IN (
            SELECT id FROM analyses
            WHERE analysis_type IN ('ITEM_DELETE', 'ARTEFACT_DELETE')
        )
    """))
    op.execute(sa.text(
        "DELETE FROM analyses "
        "WHERE analysis_type IN ('ITEM_DELETE', 'ARTEFACT_DELETE')"
    ))

# vim: ts=4 sw=4 et
