"""
Arcology - Analysis Blueprint

View and manage analysis jobs.
"""

import re

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required

from ..extensions import db
from ..database import Analysis, AnalysisStatus

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/analysis', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Analysis", f"{ROUTENAME}.index", 300)


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
    
    page = request.args.get('page', 1, type=int)
    pagination = query.order_by(Analysis.created_at.desc()).paginate(page=page, per_page=50)
    
    status_counts = {
        'pending': Analysis.query.filter(Analysis.status == AnalysisStatus.PENDING).count(),
        'running': Analysis.query.filter(Analysis.status == AnalysisStatus.RUNNING).count(),
        'completed': Analysis.query.filter(Analysis.status == AnalysisStatus.COMPLETED).count(),
        'failed': Analysis.query.filter(Analysis.status == AnalysisStatus.FAILED).count(),
    }
    
    return render_template('analysis/index.html',
                           analyses=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter,
                           status_counts=status_counts)


@blueprint.route('/<string:uuid>')
@login_required
def view(uuid):
    """View analysis details."""
    analysis = Analysis.query.filter_by(uuid=uuid).first_or_404()
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
def cancel(uuid):
    """Cancel a pending analysis."""
    analysis = Analysis.query.filter_by(uuid=uuid).first_or_404()

    if analysis.status != AnalysisStatus.PENDING:
        flash('Can only cancel pending analyses.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid))

    db.session.delete(analysis)
    db.session.commit()

    flash('Analysis cancelled.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<string:uuid>/retry', methods=['POST'])
@login_required
def retry(uuid):
    """Retry a failed analysis."""
    analysis = Analysis.query.filter_by(uuid=uuid).first_or_404()

    if analysis.status != AnalysisStatus.FAILED:
        flash('Can only retry failed analyses.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid))

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

    db.session.commit()

    flash('Analysis requeued.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', uuid=uuid))


@blueprint.route('/queue')
@login_required
def queue():
    """View the analysis queue (pending and running)."""
    pending = Analysis.query.filter(
        Analysis.status == AnalysisStatus.PENDING
    ).order_by(Analysis.created_at).all()
    
    running = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING
    ).order_by(Analysis.started_at).all()
    
    return render_template('analysis/queue.html', pending=pending, running=running)


# vim: ts=4 sw=4 noet
