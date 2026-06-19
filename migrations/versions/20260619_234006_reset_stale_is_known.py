"""Reset stale is_known on extracted files whose known_file_id is NULL

When a KnownFile is deleted, the ``extracted_files.known_file_id`` FK
(``ON DELETE SET NULL``) nulls the link but cannot touch the sibling
``is_known`` flag, so rows deleted before the app-level resets existed (and any
that slip through the FK in future) are left with ``is_known = TRUE`` and a NULL
``known_file_id``.  That mismatch crashes the file-listing template, which trusts
``is_known`` and then dereferences ``file.known_file.database_id`` on ``None``.

This data-correction migration clears the flag on any such orphaned rows.  (The
template is also hardened to render the badge only when the relationship
resolves, so the crash cannot recur even if a row goes stale again.)

Revision ID: 00006a35d356
Revises: 00006a332631
Create Date: 2026-06-19 23:40:06.000000
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '00006a35d356'
down_revision = '00006a332631'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "UPDATE extracted_files SET is_known = :f "
        "WHERE known_file_id IS NULL AND is_known = :t"
    ).bindparams(f=False, t=True))


def downgrade():
    # Data correction only — the previous (inconsistent) state is not recoverable
    # and would not be desirable to restore, so the downgrade is a no-op.
    pass

# vim: ts=4 sw=4 et
