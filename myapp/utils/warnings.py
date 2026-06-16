"""Capture Python ``warnings`` (notably SQLAlchemy ``SAWarning``) so they are
visible instead of silently printed to stderr and lost.

Background
----------
SQLAlchemy reports problems such as a *cartesian product* in a query by calling
``warnings.warn(..., SAWarning)`` — **not** by raising an exception or emitting a
log record.  As a result such warnings are invisible to:

* **CI** — ``unittest`` does not fail on them unless warnings are escalated to
  errors (we do that in the workflow with ``-W error::sqlalchemy.exc.SAWarning``).
* **Sentry** — the Flask / SQLAlchemy integrations capture exceptions and query
  spans, never ``warnings``.
* **the logs** — nothing routes the ``warnings`` stream into the logging system,
  and CPython dedupes each warning to once-per-call-site.

``install_warning_capture()`` fixes the runtime side.  Log visibility is
**unconditional** (it works with or without Sentry); Sentry event forwarding is
layered on top when a DSN is configured.
"""

import logging
import warnings
from sqlalchemy.exc import SAWarning

_installed = False


def install_warning_capture(report_to_sentry=False):
    """Route Python warnings into logging (always) and Sentry (optionally).

    Idempotent: safe to call from ``create_app()`` even though the test suite
    builds many apps in one process — only the first call installs anything.

    :param report_to_sentry: when true, ``SAWarning`` (and subclasses) are also
        reported to Sentry as ``warning``-level events.  Requires ``sentry_sdk``
        to already be initialised by the caller.
    """
    global _installed
    if _installed:
        return
    _installed = True

    # Runtime visibility (option 3): send the `warnings` stream through the
    # logging system so warnings reach the app / gunicorn logs instead of bare
    # stderr (which gunicorn may swallow).  This installs logging's showwarning
    # hook, which logs each warning to the `py.warnings` logger at WARNING.
    logging.captureWarnings(True)

    # Defeat CPython's once-per-call-site dedup for SAWarning so recurrences keep
    # surfacing in long-lived web/worker processes.  Appended (lowest priority)
    # so a CI `-W error::sqlalchemy.exc.SAWarning` filter still wins and fails
    # the build.
    warnings.filterwarnings('always', category=SAWarning, append=True)

    if not report_to_sentry:
        return

    import sentry_sdk

    # captureWarnings(True) replaced warnings.showwarning with logging's version.
    # Chain ours in front of it so warnings are still logged AND, for SAWarning,
    # also reported to Sentry as events (the SqlalchemyIntegration only captures
    # exceptions and query spans, never `warnings`).
    _chained_showwarning = warnings.showwarning

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        _chained_showwarning(message, category, filename, lineno, file, line)
        if issubclass(category, SAWarning):
            # Message string carries the origin so the event is actionable even
            # without scope APIs (which differ across sentry-sdk 1.x / 2.x).
            sentry_sdk.capture_message(
                f'{category.__name__}: {message} ({filename}:{lineno})',
                level='warning',
            )

    warnings.showwarning = _showwarning


# vim: ts=4 sw=4 et
