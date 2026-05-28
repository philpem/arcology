"""Rename 'curator' share permission to 'editor'; add 'curator' as top tier

Revision ID: 00006a17aed5
Revises: 00006a1577fd
Create Date: 2026-05-28

Three-tier share permission model:
  viewer  — read-only access
  editor  — can add/modify content (was 'curator')
  curator — full co-curation rights: privacy toggle, share management
"""
import sqlalchemy as sa
from alembic import op

revision = '00006a17aed5'
down_revision = '00006a1577fd'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "UPDATE item_shares SET permission = 'editor' WHERE permission = 'curator'"
    ))


def downgrade():
    # 'editor' did not exist before this migration; remap back to 'curator'.
    # True curator shares (introduced by this migration) are downgraded to
    # 'curator' as well — they will have elevated privileges after rollback,
    # but that is the safer direction (no access is lost).
    op.execute(sa.text(
        "UPDATE item_shares SET permission = 'curator' WHERE permission IN ('editor', 'curator')"
    ))

# vim: ts=4 sw=4 et
