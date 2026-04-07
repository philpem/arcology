"""
Arcology - Analysis Blueprint

View and manage analysis jobs.
"""

import re
from datetime import datetime, timezone, timedelta

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..database import Analysis, AnalysisStatus, Artefact
from ..permissions import require_permission
from ..utils.pagination import resolve_per_page, VALID_PER_PAGE

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/analysis', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Analysis", f"{ROUTENAME}.queue", 300)


def _get_analysis_or_404(uuid):
    """Load an analysis by UUID."""
    return Analysis.query.filter_by(uuid=uuid).first_or_404()


def _view_redirect(analysis):
    """Redirect to the analysis detail page."""
    return redirect(url_for(f'{ROUTENAME}.view', uuid=analysis.uuid))


def _require_analysis_status(analysis, expected_status, message):
    """Return a redirect response if the analysis is not in the expected state."""
    if analysis.status != expected_status:
        flash(message, 'error')
        return _view_redirect(analysis)
    return None


def _reset_for_retry(analysis):
    """Clear worker-populated fields so the job can be re-queued cleanly."""
    analysis.status = AnalysisStatus.PENDING
    analysis.error_message = None
    analysis.started_at = None
    analysis.completed_at = None
    analysis.tool_name = None
    analysis.tool_version = None
    analysis.output_url = None
    analysis.output_path = None
    analysis.success = None
    analysis.summary = None
    analysis.details = None


def _status_sort_order():
    """CASE expression for ordering analyses: running → pending → failed → completed."""
    return case(
        (Analysis.status == AnalysisStatus.RUNNING, 0),
        (Analysis.status == AnalysisStatus.PENDING, 1),
        (Analysis.status == AnalysisStatus.FAILED, 2),
        else_=3,
    )


def _stale_cutoff():
    """Return the datetime before which a RUNNING job is considered stuck."""
    seconds = current_app.config.get('STALE_JOB_TIMEOUT_SECONDS', 3600)
    # started_at is stored as naive UTC; compare against naive UTC now
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


@blueprint.route('/')
@login_required
def index():
    """List all analysis jobs."""
    status_filter = request.args.get('status')
    
    query = Analysis.query

    if status_filter:
        try:
            status = AnalysisStatus(status_filter)
            query = query.filter(Analysis.status == status)
        except ValueError:
            pass

    per_page, page, view_all = resolve_per_page('ANALYSES_PER_PAGE', 50)

    # Eager-load artefact to avoid N+1 lazy loads in template
    pagination = query.options(
        joinedload(Analysis.artefact)
    ).order_by(_status_sort_order(), Analysis.created_at.desc()).paginate(page=page, per_page=per_page)

    # Single query for all status counts using conditional aggregation
    counts_row = db.session.query(
        func.count(case((Analysis.status == AnalysisStatus.PENDING, 1))).label('pending'),
        func.count(case((Analysis.status == AnalysisStatus.RUNNING, 1))).label('running'),
        func.count(case((Analysis.status == AnalysisStatus.COMPLETED, 1))).label('completed'),
        func.count(case((Analysis.status == AnalysisStatus.FAILED, 1))).label('failed'),
    ).one()
    status_counts = {
        'pending': counts_row.pending,
        'running': counts_row.running,
        'completed': counts_row.completed,
        'failed': counts_row.failed,
    }

    return render_template('analysis/index.html',
                           analyses=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter,
                           status_counts=status_counts,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all)


@blueprint.route('/artefact/<string:uuid>')
@login_required
def artefact_analyses(uuid):
    """List all analyses for an artefact and its derived artefacts."""
    from .artefacts import get_all_derived_artefact_ids

    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)

    query = Analysis.query.filter(
        Analysis.artefact_id.in_(all_artefact_ids)
    ).options(joinedload(Analysis.artefact))

    status_filter = request.args.get('status')
    if status_filter:
        try:
            status = AnalysisStatus(status_filter)
            query = query.filter(Analysis.status == status)
        except ValueError:
            status_filter = None

    # Status counts across all analyses for this artefact (unfiltered)
    counts_row = db.session.query(
        func.count(case((Analysis.status == AnalysisStatus.PENDING, 1))).label('pending'),
        func.count(case((Analysis.status == AnalysisStatus.RUNNING, 1))).label('running'),
        func.count(case((Analysis.status == AnalysisStatus.COMPLETED, 1))).label('completed'),
        func.count(case((Analysis.status == AnalysisStatus.FAILED, 1))).label('failed'),
    ).filter(Analysis.artefact_id.in_(all_artefact_ids)).one()
    status_counts = {
        'pending': counts_row.pending,
        'running': counts_row.running,
        'completed': counts_row.completed,
        'failed': counts_row.failed,
    }

    per_page, page, view_all = resolve_per_page('ANALYSES_PER_PAGE', 50)
    pagination = query.order_by(Analysis.created_at.desc()).paginate(page=page, per_page=per_page)

    return render_template('analysis/artefact.html',
                           artefact=artefact,
                           analyses=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter,
                           status_counts=status_counts,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all)


@blueprint.route('/<string:uuid>')
@login_required
def view(uuid):
    """View analysis details."""
    analysis = _get_analysis_or_404(uuid)
    # Sanitize any JSON-escaped Python surrogates (\udcNN) in stored details.
    # These arise from Acorn filenames with raw Latin-1 bytes (e.g. 0xA0 hard
    # space) appearing in the command field of process_output records written
    # before get_process_output started sanitising the command string.  Replace
    # \udcNN with \u00NN (the Latin-1 Unicode equivalent) so the template can
    # render them without triggering a UnicodeEncodeError in Werkzeug.
    if analysis.details:
        analysis.details = re.sub(
            r'\\udc([0-9a-f]{2})',
            lambda m: f'\\u00{m.group(1)}',
            analysis.details
        )
    return render_template('analysis/view.html', analysis=analysis)


@blueprint.route('/<string:uuid>/cancel', methods=['POST'])
@login_required
@require_permission('read_write')
def cancel(uuid):
    """Cancel a pending analysis."""
    analysis = _get_analysis_or_404(uuid)
    wrong_status = _require_analysis_status(
        analysis,
        AnalysisStatus.PENDING,
        'Can only cancel pending analyses.',
    )
    if wrong_status:
        return wrong_status

    db.session.delete(analysis)
    db.session.commit()

    flash('Analysis cancelled.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/reset-stale', methods=['POST'])
@login_required
@require_permission('read_write')
def reset_stale():
    """Reset RUNNING jobs that have been stuck longer than the stale timeout back to PENDING."""
    cutoff = _stale_cutoff()
    stale = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING,
        Analysis.started_at < cutoff,
    ).all()
    for analysis in stale:
        _reset_for_retry(analysis)
    db.session.commit()
    flash(f'Reset {len(stale)} stale job(s) back to pending.', 'success' if stale else 'info')
    return redirect(url_for(f'{ROUTENAME}.queue'))


@blueprint.route('/<string:uuid>/retry', methods=['POST'])
@login_required
@require_permission('read_write')
def retry(uuid):
    """Retry a failed analysis."""
    analysis = _get_analysis_or_404(uuid)
    wrong_status = _require_analysis_status(
        analysis,
        AnalysisStatus.FAILED,
        'Can only retry failed analyses.',
    )
    if wrong_status:
        return wrong_status

    _reset_for_retry(analysis)

    db.session.commit()

    flash('Analysis requeued.', 'success')
    return _view_redirect(analysis)


QUEUE_DISPLAY_LIMIT = 100


@blueprint.route('/queue')
@login_required
def queue():
    """View the analysis queue (pending and running)."""
    pending_total = Analysis.query.filter(
        Analysis.status == AnalysisStatus.PENDING
    ).count()
    running_total = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING
    ).count()

    pending = Analysis.query.filter(
        Analysis.status == AnalysisStatus.PENDING
    ).options(joinedload(Analysis.artefact)).order_by(Analysis.created_at).limit(QUEUE_DISPLAY_LIMIT).all()

    running = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING
    ).options(joinedload(Analysis.artefact)).order_by(Analysis.started_at).limit(QUEUE_DISPLAY_LIMIT).all()

    cutoff = _stale_cutoff()
    return render_template('analysis/queue.html', pending=pending, running=running,
                           pending_total=pending_total, running_total=running_total,
                           queue_limit=QUEUE_DISPLAY_LIMIT, stale_cutoff=cutoff)


# vim: ts=4 sw=4 et
