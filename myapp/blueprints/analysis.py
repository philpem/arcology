"""
Arcology - Analysis Blueprint

View and manage analysis jobs.
"""

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


@blueprint.route('/<int:id>')
@login_required
def view(id):
    """View analysis details."""
    analysis = Analysis.query.get_or_404(id)
    return render_template('analysis/view.html', analysis=analysis)


@blueprint.route('/<int:id>/cancel', methods=['POST'])
@login_required
def cancel(id):
    """Cancel a pending analysis."""
    analysis = Analysis.query.get_or_404(id)
    
    if analysis.status != AnalysisStatus.PENDING:
        flash('Can only cancel pending analyses.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', id=id))
    
    db.session.delete(analysis)
    db.session.commit()
    
    flash('Analysis cancelled.', 'success')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<int:id>/retry', methods=['POST'])
@login_required
def retry(id):
    """Retry a failed analysis."""
    analysis = Analysis.query.get_or_404(id)
    
    if analysis.status != AnalysisStatus.FAILED:
        flash('Can only retry failed analyses.', 'error')
        return redirect(url_for(f'{ROUTENAME}.view', id=id))
    
    analysis.status = AnalysisStatus.PENDING
    analysis.error_message = None
    analysis.started_at = None
    analysis.completed_at = None
    
    db.session.commit()
    
    flash('Analysis requeued.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view', id=id))


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
