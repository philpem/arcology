"""Shared time helpers."""

from datetime import datetime, timezone


def naive_utc_now() -> datetime:
    """Current UTC time as a naive datetime.

    Several columns (``started_at``, ``completed_at``, ``progress_updated_at``,
    ``WorkerHeartbeat.last_seen`` …) store naive UTC, and comparisons against
    them must use the same form.  Single source for that conversion so the
    pattern cannot drift across modules.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc_from_timestamp(timestamp: float) -> datetime:
    """Convert a POSIX timestamp to a naive UTC datetime.

    The tz-aware equivalent of the deprecated ``datetime.utcfromtimestamp``;
    used where a naive UTC value (matching the DB columns) is required.
    """
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)

# vim: ts=4 sw=4 et
