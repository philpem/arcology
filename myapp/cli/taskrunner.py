import click
from flask import current_app
from ..taskrunner import TaskRunner


@click.command('taskrunner')
def taskrunner():
    """Run the in-process task runner for DB-only maintenance jobs.

    A long-running, worker-style poll loop (intended for its own container) that
    runs inside the Flask app context with direct database access.  It owns the
    control-plane analyses — HASH_RESCAN, PRODUCT_RECOGNITION, and the HASHDB_LINK
    / HASHDB_DELETE / HASHDB_RECOGNITION hash-database jobs — running them
    end-to-end in-process with no HTTP round-trips to the web API, plus time-based
    periodic maintenance (stale-job reset, chunked-upload GC, scheduled
    similarity rebuild).

    Run a single instance:

      docker compose exec web flask taskrunner   # ad hoc
      # or as a dedicated `taskrunner` compose service

    Exits cleanly on SIGTERM/SIGINT after the current job finishes.
    """
    TaskRunner(current_app._get_current_object()).run()

# vim: ts=4 sw=4 et
