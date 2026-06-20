"""Drop the denormalised is_known column from extracted_files

``is_known`` was a stored boolean mirroring ``known_file_id IS NOT NULL``.  The
two could diverge — a deleted KnownFile nulls ``known_file_id`` via
ON DELETE SET NULL but could leave ``is_known`` stuck True (the bug fixed by the
preceding data-correction migration ``00006a35d356``).  ``is_known`` is now a
read-only hybrid property derived from ``known_file_id`` in the ORM, so the
stored column (and its indexes) are removed entirely; divergence is impossible.

The composite ``(partition_id, is_known)`` index is replaced by
``(partition_id, known_file_id)`` so the "unknown files in this partition"
queries (`known_file_id IS NULL`) stay index-friendly.

Revision ID: 00006a364e90
Revises: 00006a35d356
Create Date: 2026-06-20 08:25:52.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a364e90'
down_revision = '00006a35d356'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = None
    if bind.dialect.name == 'sqlite':
        # Tests build the schema from the models, not from migrations, so this
        # path is only exercised by a real SQLite-backed upgrade run.
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(bind)
        existing = {ix['name'] for ix in insp.get_indexes('extracted_files')}
    else:
        existing = None

    def _index_exists(name):
        return existing is None or name in existing

    if _index_exists('ix_extracted_files_partition_known'):
        op.drop_index('ix_extracted_files_partition_known', table_name='extracted_files')
    if _index_exists('ix_extracted_files_is_known'):
        op.drop_index('ix_extracted_files_is_known', table_name='extracted_files')

    op.create_index('ix_extracted_files_partition_known_file', 'extracted_files',
                    ['partition_id', 'known_file_id'], unique=False)

    op.drop_column('extracted_files', 'is_known')


def downgrade():
    import sqlalchemy as sa

    op.add_column('extracted_files',
                  sa.Column('is_known', sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    # Re-derive the denormalised value from the surviving link.
    op.execute(sa.text(
        "UPDATE extracted_files SET is_known = :t WHERE known_file_id IS NOT NULL"
    ).bindparams(t=True))

    op.drop_index('ix_extracted_files_partition_known_file', table_name='extracted_files')
    op.create_index('ix_extracted_files_is_known', 'extracted_files',
                    ['is_known'], unique=False)
    op.create_index('ix_extracted_files_partition_known', 'extracted_files',
                    ['partition_id', 'is_known'], unique=False)


# vim: ts=4 sw=4 et
