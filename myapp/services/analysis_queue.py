"""Shared analysis-queue helpers.

Used by both the worker-poll API endpoints (``myapp/blueprints/api.py``) and the
in-process taskrunner (``myapp/taskrunner``) so the claim eligibility and
stale-reset semantics cannot diverge between the two consumers of the queue.
"""

from datetime import datetime, timedelta, timezone
from flask import current_app
from sqlalchemy import func, or_, select, update
from ..database import Analysis, AnalysisStatus, AnalysisType
from ..extensions import db


def pending_claimable_query():
    """Base query of PENDING analyses eligible to be claimed.

    Applies the re-analysis CLEANUP dispatch barrier: a re-analysis queues a
    CLEANUP job (delete the previous run's output) alongside the replacement
    analyses.  While that CLEANUP is still outstanding (PENDING or RUNNING), no
    *other* analysis for the same artefact may be handed out — otherwise a
    concurrent replacement could run and the CLEANUP would then delete its output
    (notably the shared outputs/.cache/<uuid> partition cache, keyed on the
    artefact).  The CLEANUP row itself is never blocked, and item-deletion
    cleanups (artefact_id IS NULL) gate nothing.  The barrier keys on
    PENDING/RUNNING only, so a terminal CLEANUP lifts it.

    Returned as a query so callers can add ``.options(...)``, type filters,
    ordering and limits.  Shared by ``get_pending_analyses`` (worker poll) and
    the taskrunner claim so the two cannot drift.
    """
    blocking_cleanup_artefacts = (
        select(Analysis.artefact_id)
        .where(
            Analysis.analysis_type == AnalysisType.CLEANUP,
            Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
            Analysis.artefact_id.isnot(None),
        )
    )
    return (
        Analysis.query
        .filter(Analysis.status == AnalysisStatus.PENDING)
        .filter(or_(
            Analysis.analysis_type == AnalysisType.CLEANUP,
            Analysis.artefact_id.is_(None),
            Analysis.artefact_id.notin_(blocking_cleanup_artefacts),
        ))
    )


def reset_stale_analyses_core():
    """Re-queue RUNNING jobs stuck longer than the stale timeout.  Returns count.

    A single atomic UPDATE rather than a read-modify-write loop: a non-atomic
    approach has a race window where a worker can commit status='completed'
    between the read and the commit, causing the reset to overwrite the
    completion and re-queue an already-finished job.

    Heartbeat-based staleness: a job is stuck only if it has shown no sign of
    life (progress update or heartbeat, else its start) for the whole timeout
    window — an actively-progressing long job keeps bumping ``progress_updated_at``
    and is therefore never reset.  Commits.
    """
    timeout_seconds = current_app.config.get('STALE_JOB_TIMEOUT_SECONDS', 3600)
    # started_at is stored as naive UTC
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=timeout_seconds)
    result = db.session.execute(
        update(Analysis)
        .where(Analysis.status == AnalysisStatus.RUNNING)
        .where(func.coalesce(Analysis.progress_updated_at, Analysis.started_at) < cutoff)
        .values(
            status=AnalysisStatus.PENDING,
            error_message=None,
            started_at=None,
            completed_at=None,
            tool_name=None,
            tool_version=None,
            output_url=None,
            output_path=None,
            success=None,
            summary=None,
            details=None,
            progress_message=None,
            progress_current=None,
            progress_total=None,
            progress_updated_at=None,
        )
    )
    db.session.commit()
    return result.rowcount

# vim: ts=4 sw=4 et
