"""Set ON DELETE SET NULL on restriction added_by_id FKs

ArtefactRestriction.added_by_id and ExtractedFileRestriction.added_by_id
referenced user.id with no ON DELETE action.  Deleting a user who had added a
restriction (the delete_user guard only checks *owned* items/artefacts, so a
curator who owns nothing passes) then raised a foreign-key violation on
PostgreSQL — a 500 that poisoned the session.  UserArtefactBypass.granted_by_id
already uses ON DELETE SET NULL; this brings the two restriction FKs in line so
the user delete succeeds and the "added by" attribution simply becomes NULL.

PostgreSQL only: SQLite can't ALTER a constraint, and its FKs are unenforced in
the test/dev configuration anyway.

Revision ID: 00006a4a48dd
Revises: 00006a4a4e5c
Create Date: 2026-07-05 13:00:00 UTC
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "00006a4a48dd"
down_revision = "00006a4a4e5c"
branch_labels = None
depends_on = None


_FKS = (
    ('artefact_restrictions', 'artefact_restrictions_added_by_id_fkey'),
    ('extracted_file_restrictions', 'extracted_file_restrictions_added_by_id_fkey'),
)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    for table, name in _FKS:
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(name, table, 'user', ['added_by_id'], ['id'],
                              ondelete='SET NULL')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    for table, name in _FKS:
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(name, table, 'user', ['added_by_id'], ['id'])

# vim: ts=4 sw=4 et
