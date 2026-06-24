"""
Arcology - Analysis Blueprint

View and manage analysis jobs.
"""

import re
from datetime import timedelta
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, func, or_
from sqlalchemy.orm import joinedload
from werkzeug.exceptions import HTTPException
from ..database import (
    ANALYSIS_PRIORITY_TIERS,
    ANALYSIS_PRIORITY_URGENT,
    Analysis,
    AnalysisStatus,
    Artefact,
    Item,
)
from ..extensions import db
from ..permissions import require_permission
from ..utils.pagination import VALID_PER_PAGE, resolve_per_page
from ..utils.timeutils import naive_utc_now
from ..visibility import artefact_visibility_clause, can_view_artefact, can_view_item

# Priority tiers offered by the reprioritise controls, as (value, label) pairs.
# Derived from the single source in database.py, minus URGENT — raising a
# re-analysis to that tier is gated separately (see can_raise_analysis_priority);
# these controls only nudge already-queued jobs within the ordinary band.
REPRIORITISE_CHOICES = [
    (value, label) for value, label in ANALYSIS_PRIORITY_TIERS
    if value != ANALYSIS_PRIORITY_URGENT
]
_REPRIORITISE_VALUES = {value for value, _label in REPRIORITISE_CHOICES}

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/analysis', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Analysis", f"{ROUTENAME}.queue", 300)


def _get_analysis_or_404(uuid):
    """Load an analysis by UUID, hiding analyses on artefacts the caller may not view.

    Analyses with no artefact (system CLEANUP jobs) are admin-only: their
    hints contain storage keys, which ordinary users have no business seeing.
    """
    analysis = Analysis.query.filter_by(uuid=uuid).first_or_404()
    if analysis.artefact is None:
        if not current_user.is_admin:
            abort(404)
    elif not can_view_artefact(analysis.artefact, current_user):
        abort(404)
    return analysis


def _require_manage_analysis(analysis):
    """Abort 403 if the caller may not mutate (cancel/retry) *analysis*.

    Reuses the artefact-content gate so analyses inherit the same write rules
    as the artefact they belong to.
    """
    from .artefacts import _require_manage_artefact_content
    if analysis.artefact is not None:
        _require_manage_artefact_content(analysis.artefact)


def _visible_analyses_query():
    """Base Analysis query filtered to artefacts the current user may view.

    Admins additionally see artefact-less system jobs (CLEANUP) so stuck
    cleanup work is visible in the queue; for everyone else the outer join
    plus the visibility clause excludes them.
    """
    query = (
        Analysis.query
        .outerjoin(Artefact, Analysis.artefact_id == Artefact.id)
        .outerjoin(Item, Artefact.item_id == Item.id)
    )
    visible = artefact_visibility_clause(current_user)
    if getattr(current_user, 'is_admin', False):
        visible = or_(visible, Analysis.artefact_id.is_(None))
    return query.filter(visible)


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
    analysis.progress_message = None
    analysis.progress_current = None
    analysis.progress_total = None
    analysis.progress_updated_at = None


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
    return naive_utc_now() - timedelta(seconds=seconds)


@blueprint.route('/')
@login_required
def index():
    """List all analysis jobs."""
    status_filter = request.args.get('status')
    artefact_filter = request.args.get('artefact', '').strip() or None

    query = _visible_analyses_query()

    if status_filter:
        try:
            status = AnalysisStatus(status_filter)
            query = query.filter(Analysis.status == status)
        except ValueError:
            pass

    if artefact_filter:
        query = query.filter(Artefact.label.ilike(f'%{artefact_filter}%'))

    per_page, page, view_all = resolve_per_page('ANALYSES_PER_PAGE', 50)

    # Eager-load artefact to avoid N+1 lazy loads in template
    pagination = query.options(
        joinedload(Analysis.artefact)
    ).order_by(_status_sort_order(), Analysis.created_at.desc()).paginate(page=page, per_page=per_page)

    # Single query for all status counts using conditional aggregation.  These
    # are global operational-load totals (matching the queue page): they expose
    # only aggregate job counts, not any identifiable content, so they are not
    # visibility-filtered even though the listed rows above are.
    counts_row = db.session.query(
        func.count(case((Analysis.status == AnalysisStatus.PENDING, 1))).label('pending'),
        func.count(case((Analysis.status == AnalysisStatus.RUNNING, 1))).label('running'),
        func.count(case((Analysis.status == AnalysisStatus.COMPLETED, 1))).label('completed'),
        func.count(case((Analysis.status == AnalysisStatus.FAILED, 1))).label('failed'),
    ).select_from(Analysis).one()
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
                           artefact_filter=artefact_filter,
                           status_counts=status_counts,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all)


@blueprint.route('/artefact/<string:uuid>')
@login_required
def artefact_analyses(uuid):
    """List all analyses for an artefact and its derived artefacts."""
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    if not can_view_artefact(artefact, current_user):
        abort(404)
    # A derived artefact may be independently marked private even when the root
    # is public, so restrict to the artefacts the caller may actually view —
    # otherwise their analyses (and the status counts below) would leak via the
    # parent's analysis page.
    all_artefact_ids = _visible_artefact_ids_for(artefact)

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
                           reprioritise_choices=REPRIORITISE_CHOICES,
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
    # Sanitised copy for display only — do not mutate the ORM object, which
    # would cause the rewritten string to be flushed to the DB on any autoflush.
    details = re.sub(
        r'\\udc([0-9a-f]{2})',
        lambda m: f'\\u00{m.group(1)}',
        analysis.details,
    ) if analysis.details else analysis.details
    return render_template('analysis/view.html', analysis=analysis, details=details,
                           reprioritise_choices=REPRIORITISE_CHOICES)


@blueprint.route('/<string:uuid>/status.json')
@login_required
def status_json(uuid):
    """Return live status/progress for one analysis (detail-page JS poller).

    Exposes only the running-progress fields the detail page needs, gated by
    the same visibility check as the detail view itself.
    """
    analysis = _get_analysis_or_404(uuid)
    return jsonify(
        status=analysis.status.value,
        progress_message=analysis.progress_message,
        progress_current=analysis.progress_current,
        progress_total=analysis.progress_total,
    )


@blueprint.route('/<string:uuid>/cancel', methods=['POST'])
@login_required
@require_permission('read_write')
def cancel(uuid):
    """Cancel a pending analysis."""
    analysis = _get_analysis_or_404(uuid)
    _require_manage_analysis(analysis)
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
@require_permission('staff')
def reset_stale():
    """Reset RUNNING jobs that have been stuck longer than the stale timeout back to PENDING."""
    cutoff = _stale_cutoff()
    stale = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING,
        func.coalesce(Analysis.progress_updated_at, Analysis.started_at) < cutoff,
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
    _require_manage_analysis(analysis)
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


def _requested_priority():
    """Parse and validate the ``priority`` form field for a reprioritise POST.

    Returns the integer priority, or ``None`` if missing/invalid/out of the
    allowed reprioritise band."""
    raw = request.form.get('priority')
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value in _REPRIORITISE_VALUES else None


def _apply_priority(query, priority):
    """Set ``priority`` on every PENDING analysis matched by *query*.  Commits.

    Only PENDING rows are touched — reprioritising a RUNNING/COMPLETED/FAILED job
    has no effect on dispatch order.  Returns the number of rows updated."""
    rows = query.filter(Analysis.status == AnalysisStatus.PENDING).all()
    for analysis in rows:
        analysis.priority = priority
    db.session.commit()
    return len(rows)


def _visible_artefact_ids(criterion):
    """Artefact ids matching *criterion* that the current user may view.

    Single source for the visibility-filtered id collection shared by the
    artefact-analyses listing and the bulk reprioritise routes."""
    return [
        row[0] for row in db.session.query(Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(criterion, artefact_visibility_clause(current_user))
        .all()
    ]


def _visible_artefact_ids_for(artefact):
    """IDs of *artefact* and its derived subtree the current user may view."""
    from ..services.artefact_lifecycle import get_all_derived_artefact_ids
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    return _visible_artefact_ids(Artefact.id.in_(all_ids))


def _manageable_item_artefact_ids(item):
    """IDs of *item*'s artefacts the caller may both view and manage content of."""
    from .artefacts import _require_manage_artefact_content
    # Eager-load item: _require_manage_artefact_content reads artefact.item, so
    # without this each artefact triggers a lazy Item load (N+1).
    candidates = (
        Artefact.query.filter(Artefact.id.in_(_visible_artefact_ids(Artefact.item_id == item.id)))
        .options(joinedload(Artefact.item)).all()
    )
    manageable = []
    for artefact in candidates:
        try:
            _require_manage_artefact_content(artefact)
        except HTTPException:
            # No content rights on this particular artefact — skip it silently
            # and reprioritise only the jobs the caller may manage.
            continue
        manageable.append(artefact.id)
    return manageable


def _bulk_reprioritise(ids_fn, redirect_resp):
    """Shared body of the bulk (artefact/item) reprioritise routes.

    Validates the submitted priority, then sets it on the PENDING analyses of
    the artefacts ``ids_fn()`` resolves to (called lazily, only once the
    priority is valid).  Returns *redirect_resp* either way."""
    priority = _requested_priority()
    if priority is None:
        flash('Invalid priority.', 'error')
        return redirect_resp
    count = _apply_priority(
        Analysis.query.filter(Analysis.artefact_id.in_(ids_fn())), priority)
    flash(f'Updated priority on {count} pending analysis(es).',
          'success' if count else 'info')
    return redirect_resp


@blueprint.route('/<string:uuid>/priority', methods=['POST'])
@login_required
@require_permission('read_write')
def set_priority(uuid):
    """Set the queue priority of a single PENDING analysis."""
    analysis = _get_analysis_or_404(uuid)
    _require_manage_analysis(analysis)
    priority = _requested_priority()
    if priority is None:
        flash('Invalid priority.', 'error')
        return _view_redirect(analysis)
    wrong_status = _require_analysis_status(
        analysis, AnalysisStatus.PENDING, 'Can only reprioritise pending analyses.')
    if wrong_status:
        return wrong_status
    analysis.priority = priority
    db.session.commit()
    flash('Analysis priority updated.', 'success')
    return _view_redirect(analysis)


@blueprint.route('/artefact/<string:uuid>/priority', methods=['POST'])
@login_required
@require_permission('read_write')
def set_artefact_priority(uuid):
    """Bulk-set priority on all PENDING analyses for an artefact + derived tree."""
    from .artefacts import _require_manage_artefact_content
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    if not can_view_artefact(artefact, current_user):
        abort(404)
    _require_manage_artefact_content(artefact)
    return _bulk_reprioritise(
        lambda: _visible_artefact_ids_for(artefact),
        redirect(url_for(f'{ROUTENAME}.artefact_analyses', uuid=uuid)))


@blueprint.route('/item/<string:uuid>/priority', methods=['POST'])
@login_required
@require_permission('read_write')
def set_item_priority(uuid):
    """Bulk-set priority on all PENDING analyses across an item's artefacts."""
    item = Item.query.filter_by(uuid=uuid).first_or_404()
    if not can_view_item(item, current_user):
        abort(404)
    return _bulk_reprioritise(
        lambda: _manageable_item_artefact_ids(item),
        redirect(url_for('myapp_blueprints_items.view', uuid=item.url_id)))


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

    # Totals stay global (queue load is operational information); the listed
    # entries are filtered to artefacts the caller may view.
    pending = _visible_analyses_query().filter(
        Analysis.status == AnalysisStatus.PENDING
    ).options(joinedload(Analysis.artefact)).order_by(Analysis.created_at).limit(QUEUE_DISPLAY_LIMIT).all()

    running = _visible_analyses_query().filter(
        Analysis.status == AnalysisStatus.RUNNING
    ).options(joinedload(Analysis.artefact)).order_by(Analysis.started_at).limit(QUEUE_DISPLAY_LIMIT).all()

    cutoff = _stale_cutoff()
    return render_template('analysis/queue.html', pending=pending, running=running,
                           pending_total=pending_total, running_total=running_total,
                           reprioritise_choices=REPRIORITISE_CHOICES,
                           queue_limit=QUEUE_DISPLAY_LIMIT, stale_cutoff=cutoff)


@blueprint.route('/queue/status.json')
@login_required
def queue_status_json():
    """Return queue counts plus per-running-job progress for the queue poller.

    Counts stay global (operational load); the per-job ``jobs`` list is
    visibility-filtered via _visible_analyses_query() and capped at
    QUEUE_DISPLAY_LIMIT so the poller can update each running row's progress
    bar in place without a full page reload.
    """
    pending = Analysis.query.filter(Analysis.status == AnalysisStatus.PENDING).count()
    running = Analysis.query.filter(Analysis.status == AnalysisStatus.RUNNING).count()
    jobs = _visible_analyses_query().filter(
        Analysis.status == AnalysisStatus.RUNNING
    ).order_by(Analysis.started_at).limit(QUEUE_DISPLAY_LIMIT).all()
    return jsonify(
        pending=pending,
        running=running,
        jobs=[{
            'id': a.id,
            'progress_message': a.progress_message,
            'progress_current': a.progress_current,
            'progress_total': a.progress_total,
        } for a in jobs],
    )


# vim: ts=4 sw=4 et
