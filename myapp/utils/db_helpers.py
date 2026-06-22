"""Shared database query helpers."""

from sqlalchemy import text
from ..extensions import db


def is_statement_timeout(exc):
    """True if *exc* is a PostgreSQL statement_timeout abort (SQLSTATE 57014)."""
    return getattr(getattr(exc, 'orig', None), 'pgcode', None) == '57014'


def apply_statement_timeout(seconds):
    """Bound the current transaction's query time (PostgreSQL only).

    ``SET LOCAL`` scopes the timeout to the active transaction, so it applies to
    the query that follows and is discarded at commit/rollback.  A non-positive
    value disables the guard.  No-op on backends without ``statement_timeout``
    (e.g. SQLite under the test suite).
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return
    if seconds <= 0:
        return
    if db.session.get_bind().dialect.name != 'postgresql':
        return
    db.session.execute(text(f'SET LOCAL statement_timeout = {seconds * 1000}'))


def is_deadlock(exc):
    """True if *exc* is a PostgreSQL deadlock abort (SQLSTATE 40P01)."""
    return getattr(getattr(exc, 'orig', None), 'pgcode', None) == '40P01'


def is_foreign_key_violation(exc):
    """True if *exc* is a PostgreSQL foreign-key violation (SQLSTATE 23503)."""
    return getattr(getattr(exc, 'orig', None), 'pgcode', None) == '23503'


def insert_ignore_conflict(model, rows, index_elements, *, batch_size=1000):
    """Bulk-insert *rows* into *model*, skipping rows that hit a unique conflict.

    ``rows`` is a list of column dicts; ``index_elements`` names the columns of
    the unique constraint to conflict on (e.g. the two columns of a
    ``uq_*_pair``).  Emits ``INSERT ... ON CONFLICT DO NOTHING`` on PostgreSQL and
    SQLite (the dialects this app runs on), so a duplicate inserted concurrently
    from the symmetric side is silently dropped rather than raising
    ``IntegrityError``.  Does not commit — the caller owns the transaction.

    Returns the number of rows **actually inserted** (conflicting rows excluded),
    so callers reporting progress counts don't overstate during a race.  The
    unsupported-backend fallback may report ``-1`` where the driver does not
    expose a row count.
    """
    if not rows:
        return 0
    dialect = db.session.get_bind().dialect.name
    inserted = 0
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # pragma: no cover - unsupported backend; fall back to a plain insert
        for i in range(0, len(rows), batch_size):
            result = db.session.execute(model.__table__.insert(), rows[i:i + batch_size])
            inserted += result.rowcount
        return inserted
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        stmt = _insert(model.__table__).values(chunk).on_conflict_do_nothing(
            index_elements=index_elements)
        inserted += db.session.execute(stmt).rowcount
    return inserted


def _query_with_options(model, *load_options):
    """Return a model query with optional eager-load directives applied."""
    query = model.query
    if load_options:
        query = query.options(*load_options)
    return query


def get_by_uuid_or_404(model, uuid, *load_options):
    """Look up a model by UUID with optional eager-load directives."""
    return _query_with_options(model, *load_options).filter_by(uuid=uuid).first_or_404()


def get_by_id_or_404(model, id, *load_options):
    """Look up a model by integer primary key with optional eager-load directives."""
    return _query_with_options(model, *load_options).filter_by(id=id).first_or_404()


def model_choice_list(model, label='-- Select --', order_field='name', exclude_ids=None):
    """Build SelectField choices from a model: [(0, label), (id, name), ...]."""
    exclude_ids = exclude_ids or set()
    order_col = getattr(model, order_field)
    return [(0, label)] + [
        (item.id, item.name)
        for item in model.query.order_by(order_col).all()
        if item.id not in exclude_ids
    ]


def normalize_hash(value):
    """Normalize a hash string: strip whitespace, lowercase, return None if empty."""
    if not value:
        return None
    result = value.strip().lower()
    return result or None

# vim: ts=4 sw=4 et
