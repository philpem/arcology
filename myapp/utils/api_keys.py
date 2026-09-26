"""One-time API key display helpers.

When a key is created the raw value must be shown exactly once.  It is kept
briefly in the (client-side, signed) session rather than the database.  Storing
the owning user alongside the key means the display view can refuse to render a
key that belongs to a different account — e.g. if the URL is edited, or an
admin's own self-service key is still pending.
"""

from flask import session

_SESSION_KEY = 'new_api_key'


def set_pending_api_key(user_id: int, raw_key: str) -> None:
    """Remember *raw_key* for one-time display to *user_id*."""
    session[_SESSION_KEY] = {'user_id': user_id, 'raw_key': raw_key}


def take_pending_api_key(user_id: int) -> str | None:
    """Return and consume the pending raw key if it belongs to *user_id*.

    Returns ``None`` (consuming nothing) when there is no pending key or it
    belongs to a different user, so a stray visit cannot discard another
    account's key.
    """
    data = session.get(_SESSION_KEY)
    if not isinstance(data, dict) or data.get('user_id') != user_id:
        return None
    session.pop(_SESSION_KEY, None)
    return data.get('raw_key')


# vim: ts=4 sw=4 et
