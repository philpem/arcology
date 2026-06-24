"""The taskrunner poll loop.

Mirrors the analysis worker's main loop (``worker/arcworker/analysis.py``) but
runs in-process with direct DB access: it atomically claims PENDING
control-plane analyses, dispatches them to in-process job functions, heartbeats
their progress, and runs time-based periodic maintenance between polls.
"""

import json
import logging
import signal
import threading
import time
from flask import current_app
from sqlalchemy import select, update
from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES
from ..database import Analysis, AnalysisStatus
from ..extensions import db
from ..services.analysis_queue import (
    gc_stale_worker_heartbeats,
    pending_claimable_query,
    reset_stale_analyses_core,
)
from ..services.chunked_upload import purge_stale_chunks
from ..services.hashdb_jobs import JobCancelled
from ..services.similarity import rebuild_all, refresh_dirty
from ..utils.timeutils import naive_utc_now
from .dispatch import DISPATCH

log = logging.getLogger('arcology.taskrunner')

# Stored timestamps are naive UTC, matching the worker / api.py convention.
_utcnow = naive_utc_now


class TaskRunner:
    """Single-instance poll loop for DB-only control-plane analysis jobs."""

    def __init__(self, app):
        self.app = app
        cfg = app.config
        self._shutdown = threading.Event()
        self.backoff_floor = float(cfg.get('TASKRUNNER_POLL_BACKOFF_FLOOR', 0.5))
        self.backoff_ceiling = float(cfg.get('TASKRUNNER_POLL_BACKOFF_CEILING', 10))

        # Periodic (time-based) maintenance: (interval_seconds, callable, name).
        # Intervals <= 0 disable the task.  monotonic last-run stamps live in
        # self._periodic_last, keyed by name.
        self._periodics = [
            (int(cfg.get('TASKRUNNER_STALE_RESET_INTERVAL', 300)),
             self._reset_stale, 'stale-reset'),
            (int(cfg.get('TASKRUNNER_CHUNK_GC_INTERVAL', 3600)),
             purge_stale_chunks, 'chunk-gc'),
            (int(cfg.get('TASKRUNNER_HEARTBEAT_GC_INTERVAL', 600)),
             gc_stale_worker_heartbeats, 'worker-heartbeat-gc'),
            (int(cfg.get('TASKRUNNER_SIMILARITY_DELTA_INTERVAL', 0)),
             self._refresh_similarity_delta, 'similarity-delta'),
            (int(cfg.get('TASKRUNNER_SIMILARITY_INTERVAL', 0)),
             self._rebuild_similarity, 'similarity-rebuild'),
        ]
        # Max stale artefacts drained per similarity-delta tick, so one sweep
        # can't monopolise the loop; the remainder is taken on later ticks.
        self.similarity_delta_max = int(cfg.get('TASKRUNNER_SIMILARITY_DELTA_MAX', 200))
        self._periodic_last = {}

    # -- signals / sleep ----------------------------------------------------

    def _install_signal_handlers(self):
        def _handle(signum, frame):
            log.info('Received signal %s, shutting down after current job', signum)
            self._shutdown.set()
        for _sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(_sig, _handle)

    def _idle_sleep(self, delay):
        # Wait on the shutdown Event so SIGTERM wakes us immediately rather than
        # after a full backoff interval (cf. the worker's _idle_sleep).
        self._shutdown.wait(timeout=delay)

    # -- per-job heartbeat / cancellation ----------------------------------

    def _make_heartbeat(self, analysis_id):
        def heartbeat(current=None, total=None, label=None):
            values = {'progress_updated_at': _utcnow()}
            if label is not None:
                values['progress_message'] = label
            if current is not None:
                values['progress_current'] = current
            if total is not None:
                values['progress_total'] = total
            db.session.execute(
                update(Analysis).where(Analysis.id == analysis_id).values(**values))
            db.session.commit()
        return heartbeat

    def _make_check_cancelled(self, analysis_id):
        def check_cancelled():
            # A UI/CLI cancel flips status away from RUNNING (e.g. to FAILED or
            # back to PENDING).  Abort cleanly and leave the row as the canceller
            # set it.
            status = db.session.execute(
                select(Analysis.status).where(Analysis.id == analysis_id)
            ).scalar_one_or_none()
            if status != AnalysisStatus.RUNNING:
                raise JobCancelled(f'analysis {analysis_id} no longer RUNNING ({status})')
        return check_cancelled

    # -- claim / complete / fail -------------------------------------------

    def _claim_one(self):
        """Atomically claim one PENDING control-plane analysis, or return None."""
        candidate = (
            # apply_heavy_cap=False: the taskrunner is single-instance and already
            # serialises to one job, so the worker-fleet heavy cap must not gate
            # its cross-domain control-plane claims.
            pending_claimable_query(apply_heavy_cap=False)
            .filter(Analysis.analysis_type.in_(CONTROL_PLANE_ANALYSIS_TYPES))
            .order_by(Analysis.priority.desc(), Analysis.created_at)
            .limit(1)
            .first()
        )
        if candidate is None:
            return None
        now = _utcnow()
        result = db.session.execute(
            update(Analysis)
            .where(Analysis.id == candidate.id, Analysis.status == AnalysisStatus.PENDING)
            .values(status=AnalysisStatus.RUNNING, started_at=now,
                    progress_updated_at=now, error_message=None)
        )
        db.session.commit()
        if result.rowcount != 1:
            return None  # lost the race to another claimant
        return db.session.get(Analysis, candidate.id)

    def _complete(self, analysis_id, result):
        # Only write if still RUNNING — a cancel that landed after the last
        # cancellation check must not be overwritten by a late completion.
        db.session.execute(
            update(Analysis)
            .where(Analysis.id == analysis_id, Analysis.status == AnalysisStatus.RUNNING)
            .values(
                status=AnalysisStatus.COMPLETED,
                success=True,
                completed_at=_utcnow(),
                summary=result.get('summary'),
                details=json.dumps(result, default=str),
            )
        )
        db.session.commit()

    def _fail(self, analysis_id, message):
        db.session.execute(
            update(Analysis)
            .where(Analysis.id == analysis_id, Analysis.status == AnalysisStatus.RUNNING)
            .values(
                status=AnalysisStatus.FAILED,
                success=False,
                completed_at=_utcnow(),
                error_message=message,
            )
        )
        db.session.commit()

    def claim_and_process(self):
        """Claim and run one job.  Returns 1 if a job was processed, else 0."""
        analysis = self._claim_one()
        if analysis is None:
            return 0
        analysis_id = analysis.id
        analysis_type = analysis.analysis_type
        log.info('Claimed %s analysis %s', analysis_type.name, analysis_id)
        handler = DISPATCH.get(analysis_type)
        if handler is None:  # defensive: assert in dispatch.py prevents this
            self._fail(analysis_id, f'No taskrunner handler for {analysis_type.name}')
            return 1
        heartbeat = self._make_heartbeat(analysis_id)
        check_cancelled = self._make_check_cancelled(analysis_id)
        try:
            result = handler(analysis, heartbeat=heartbeat, check_cancelled=check_cancelled)
        except JobCancelled as exc:
            log.info('Analysis %s cancelled: %s', analysis_id, exc)
            db.session.rollback()
            return 1  # leave the row as the canceller set it
        except Exception as exc:
            log.exception('Analysis %s failed', analysis_id)
            db.session.rollback()
            self._fail(analysis_id, str(exc)[:2000])
            return 1
        self._complete(analysis_id, result)
        log.info('Completed %s analysis %s: %s',
                 analysis_type.name, analysis_id, result.get('summary'))
        return 1

    # -- periodic maintenance ----------------------------------------------

    def _reset_stale(self):
        count = reset_stale_analyses_core()
        if count:
            log.info('Reset %d stale analysis job(s) to PENDING', count)

    def _refresh_similarity_delta(self):
        stats = refresh_dirty(max_artefacts=self.similarity_delta_max)
        if stats['artefacts']:
            log.info('Similarity delta refresh: %s', stats)

    def _rebuild_similarity(self):
        log.info('Starting scheduled similarity rebuild')
        stats = rebuild_all()
        log.info('Similarity rebuild complete: %s', stats)

    def _run_periodics(self):
        for interval, fn, name in self._periodics:
            if interval <= 0:
                continue
            last = self._periodic_last.get(name)
            if last is not None and (time.monotonic() - last) < interval:
                continue
            self._periodic_last[name] = time.monotonic()
            try:
                fn()
            except Exception:
                db.session.rollback()
                log.exception('Periodic task %s failed', name)

    # -- main loop ----------------------------------------------------------

    def run(self):
        self._install_signal_handlers()
        log.info('Starting Arcology taskrunner')
        log.info('Owns analysis types: %s',
                 ', '.join(sorted(t.name for t in CONTROL_PLANE_ANALYSIS_TYPES)))

        current_delay = self.backoff_floor
        while not self._shutdown.is_set():
            try:
                self._run_periodics()
                if self._shutdown.is_set():
                    break
                processed = self.claim_and_process()
                if self._shutdown.is_set():
                    break
                if processed == 0:
                    self._idle_sleep(current_delay)
                    current_delay = min(current_delay * 2, self.backoff_ceiling)
                else:
                    current_delay = self.backoff_floor
            except Exception:
                # Never let one bad iteration kill the loop; roll back any
                # half-applied transaction and back off before retrying.
                log.exception('Unexpected error in taskrunner loop')
                db.session.rollback()
                self._idle_sleep(self.backoff_ceiling)

        log.info('Taskrunner shut down cleanly')


def run_taskrunner():
    """Entry point used by the ``flask taskrunner`` CLI command."""
    app = current_app._get_current_object()
    TaskRunner(app).run()

# vim: ts=4 sw=4 et
