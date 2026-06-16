"""Add pg_trgm extension and GIN trigram indexes for search ILIKE performance

Revision ID: 00006a314d69
Revises: 00006a2ff48a
Create Date: 2026-06-16

"""
import sqlalchemy as sa
from alembic import op

revision = '00006a314d69'
down_revision = '00006a2ff48a'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # pg_trgm cannot be created inside a transaction
    with op.get_context().autocommit_block():
        op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))

    # GIN trigram indexes on all columns searched with ILIKE in search.py.
    # Regular CREATE INDEX (not CONCURRENTLY) is fine inside a transaction.
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_extracted_files_filename '
        'ON extracted_files USING GIN (filename gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_extracted_files_path '
        'ON extracted_files USING GIN (path gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_partitions_label '
        'ON partitions USING GIN (label gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_partitions_gnu_file_type '
        'ON partitions USING GIN (gnu_file_type gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_items_name '
        'ON items USING GIN (name gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_items_description '
        'ON items USING GIN (description gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_artefacts_label '
        'ON artefacts USING GIN (label gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_artefacts_description '
        'ON artefacts USING GIN (description gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_riscos_modules_title_string '
        'ON riscos_modules USING GIN (title_string gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_riscos_modules_help_title '
        'ON riscos_modules USING GIN (help_title gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_riscos_modules_commands '
        'ON riscos_modules USING GIN (commands gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_riscos_modules_swi_names '
        'ON riscos_modules USING GIN (swi_names gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_tags_name '
        'ON tags USING GIN (name gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_replay_movies_title '
        'ON replay_movies USING GIN (title gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_replay_movies_author '
        'ON replay_movies USING GIN (author gin_trgm_ops)'
    ))
    op.execute(sa.text(
        'CREATE INDEX IF NOT EXISTS ix_trgm_replay_movies_copyright '
        'ON replay_movies USING GIN (copyright gin_trgm_ops)'
    ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    for idx in (
        'ix_trgm_extracted_files_filename',
        'ix_trgm_extracted_files_path',
        'ix_trgm_partitions_label',
        'ix_trgm_partitions_gnu_file_type',
        'ix_trgm_items_name',
        'ix_trgm_items_description',
        'ix_trgm_artefacts_label',
        'ix_trgm_artefacts_description',
        'ix_trgm_riscos_modules_title_string',
        'ix_trgm_riscos_modules_help_title',
        'ix_trgm_riscos_modules_commands',
        'ix_trgm_riscos_modules_swi_names',
        'ix_trgm_tags_name',
        'ix_trgm_replay_movies_title',
        'ix_trgm_replay_movies_author',
        'ix_trgm_replay_movies_copyright',
    ):
        op.execute(sa.text(f'DROP INDEX IF EXISTS {idx}'))

    # Leave the pg_trgm extension — other things may depend on it.

# vim: ts=4 sw=4 et
