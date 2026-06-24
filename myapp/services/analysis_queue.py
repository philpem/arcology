"""Shared analysis-queue helpers.

Used by both the worker-poll API endpoints (``myapp/blueprints/api.py``) and the
in-process taskrunner (``myapp/taskrunner``) so the claim eligibility and
stale-reset semantics cannot diverge between the two consumers of the queue.
"""

from datetime import timedelta
from flask import current_app
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from arcology_shared.enums import HEAVY_ANALYSIS_TYPES
from ..database import Analysis, AnalysisStatus, AnalysisType, WorkerHeartbeat
from ..extensions import db
from ..utils.timeutils import naive_utc_now


def _heartbeat_window_seconds():
    """Freshness window (seconds) for counting a worker heartbeat as live."""
    return current_app.config.get('ANALYSIS_WORKER_HEARTBEAT_WINDOW', 60)


def _heartbeat_min_write_interval():
    """Minimum seconds between rewrites of a worker's heartbeat row.

    Bounds the write rate on the hot poll path: a row seen more recently than
    this is left untouched.  Kept comfortably below the freshness window so a
    live worker's row never ages out of active_worker_count() between writes."""
    return max(5, _heartbeat_window_seconds() // 2)


def record_worker_heartbeat(worker_id, hostname=None):
    """Best-effort upsert of the liveness record for *worker_id*.

    Called from the worker poll and progress-heartbeat paths so both idle and
    busy workers are counted (one row per id).  Rate-limited (a row seen within
    _heartbeat_min_write_interval() is left untouched, skipping the write and
    its commit) and fully best-effort: any failure is rolled back and logged,
    never propagated, so a heartbeat hiccup cannot fail the worker's poll or
    status update.  Call only when the session has no other uncommitted work,
    since the rollback-on-error would also discard that."""
    if not worker_id:
        return
    worker_id = str(worker_id)[:64]
    now = naive_utc_now()
    try:
        row = db.session.get(WorkerHeartbeat, worker_id)
        if row is not None and row.last_seen is not None and (
                now - row.last_seen).total_seconds() < _heartbeat_min_write_interval():
            return  # seen recently — skip the write (and its commit)
        if row is None:
            db.session.add(WorkerHeartbeat(
                worker_id=worker_id, first_seen=now, last_seen=now,
                hostname=(hostname or None)))
        else:
            row.last_seen = now
            if hostname:
                row.hostname = hostname
        db.session.commit()
    except IntegrityError:
        # Two requests raced to insert the same new worker_id; the other won.
        # Benign — the row now exists, so there's nothing to record or log.
        db.session.rollback()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            'Failed to record worker heartbeat for %r', worker_id, exc_info=True)


def active_worker_count():
    """Number of workers seen within the heartbeat freshness window.

    Stale rows (a stopped/restarted worker) drop out of the count the moment
    they age past the window, independent of the physical GC."""
    cutoff = naive_utc_now() - timedelta(seconds=_heartbeat_window_seconds())
    return db.session.scalar(
        select(func.count()).select_from(WorkerHeartbeat)
        .where(WorkerHeartbeat.last_seen > cutoff)
    ) or 0


def gc_stale_worker_heartbeats(multiple=10):
    """Delete heartbeat rows older than *multiple* × the freshness window.

    Table hygiene only — the active-worker count already filters by freshness,
    so this just bounds the row count across worker restarts.  Returns the
    number of rows deleted.  Commits."""
    cutoff = naive_utc_now() - timedelta(seconds=_heartbeat_window_seconds() * multiple)
    result = db.session.execute(
        WorkerHeartbeat.__table__.delete().where(WorkerHeartbeat.last_seen < cutoff)
    )
    db.session.commit()
    return result.rowcount


def resolve_heavy_cap():
    """Resolve ANALYSIS_HEAVY_RUNNING_CAP to a concrete heavy-job limit.

    Accepts a positive int (absolute cap), a ``"NN%"`` string (percentage of the
    live worker count), a negative int ``-K`` (keep K worker slots free), or 0
    (disabled).  Returns the integer cap (>= 1) or ``None`` when no effective
    throttle applies — disabled, the cap meets/exceeds the live worker count, or
    a relative expression resolved against an empty worker registry (nothing is
    running, so don't throttle)."""
    raw = current_app.config.get('ANALYSIS_HEAVY_RUNNING_CAP', -1)
    pct = None
    value = None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.endswith('%'):
            try:
                pct = float(s[:-1])
            except ValueError:
                return None
        else:
            try:
                value = int(s)
            except ValueError:
                return None
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None

    # Worker count is needed by the relative forms and by the final
    # "no effective throttle" check; fetch it once (this runs on every worker
    # poll, so avoid repeating the COUNT query).
    workers = active_worker_count()

    if pct is not None:
        if workers <= 0:
            return None
        cap = max(1, round(pct / 100.0 * workers))
    elif value == 0:
        return None
    elif value > 0:
        cap = value
    else:  # value < 0 -> keep abs(value) slots free for light jobs
        if workers <= 0:
            return None
        cap = max(1, workers + value)

    # No effective throttle if the cap is at or above the live worker count.
    if workers and cap >= workers:
        return None
    return cap


def pending_claimable_query(apply_heavy_cap=True):
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

    When ``apply_heavy_cap`` is set (the default, used by the worker poll) and a
    cap resolves (see ``resolve_heavy_cap``), a fairness filter additionally
    withholds heavy analyses once enough are already RUNNING, so a burst of heavy
    jobs cannot occupy every worker and starve quick jobs.  The taskrunner passes
    ``apply_heavy_cap=False``: it is single-instance and serialises to one job, so
    capping its (cross-domain control-plane) claims against worker heavy load
    would only deadlock unrelated work.

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
    query = (
        Analysis.query
        .filter(Analysis.status == AnalysisStatus.PENDING)
        .filter(or_(
            Analysis.analysis_type == AnalysisType.CLEANUP,
            Analysis.artefact_id.is_(None),
            Analysis.artefact_id.notin_(blocking_cleanup_artefacts),
        ))
    )

    if apply_heavy_cap:
        cap = resolve_heavy_cap()
        if cap is not None:
            heavy_types = list(HEAVY_ANALYSIS_TYPES)
            running_heavy = (
                select(func.count()).select_from(Analysis)
                .where(
                    Analysis.status == AnalysisStatus.RUNNING,
                    Analysis.analysis_type.in_(heavy_types),
                ).scalar_subquery()
            )
            # Best-effort: the atomic PENDING->RUNNING claim is the only hard
            # gate, so concurrent workers can briefly exceed the cap by up to the
            # worker count.  Anti-starvation falls out of cap >= 1 (resolve_heavy_cap
            # clamps it): when nothing heavy is running, ``running_heavy < cap`` is
            # ``0 < cap``, always true, so an all-heavy queue still drains.
            query = query.filter(or_(
                Analysis.analysis_type.notin_(heavy_types),
                running_heavy < cap,
            ))

    return query


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
    cutoff = naive_utc_now() - timedelta(seconds=timeout_seconds)
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
