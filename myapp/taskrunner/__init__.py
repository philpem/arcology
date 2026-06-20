"""In-process task runner for the DB-only control-plane analysis jobs.

The taskrunner is a worker-style poll loop (``flask taskrunner``) that runs
inside the Flask app context with direct database access.  It owns the
``CONTROL_PLANE_ANALYSIS_TYPES`` jobs — historically driven by the analysis
worker looping bounded HTTP step endpoints — and runs them end-to-end
in-process with no HTTP round-trips, plus a handful of time-based periodic
maintenance tasks.
"""

from .runner import TaskRunner

__all__ = ['TaskRunner']

# vim: ts=4 sw=4 et
