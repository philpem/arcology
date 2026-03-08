"""
Arcology - Dashboard Blueprint

Homepage and dashboard views.
"""

from flask import Blueprint, render_template
from flask_login import login_required
from sqlalchemy import func

from ..extensions import db
from ..database import Item, Artefact, Analysis, AnalysisStatus

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Dashboard", f"{ROUTENAME}.index", -1000)


@blueprint.route("/")
@login_required
def index():
    """Homepage with dashboard statistics."""
    stats = {
        'total_items': db.session.query(func.count(Item.id)).scalar() or 0,
        'total_artefacts': db.session.query(func.count(Artefact.id)).scalar() or 0,
        'pending_analyses': db.session.query(func.count(Analysis.id)).filter(
            Analysis.status == AnalysisStatus.PENDING
        ).scalar() or 0,
        'running_analyses': db.session.query(func.count(Analysis.id)).filter(
            Analysis.status == AnalysisStatus.RUNNING
        ).scalar() or 0,
    }
    
    recent_items = Item.query.order_by(Item.created_at.desc()).limit(10).all()
    recent_analyses = Analysis.query.order_by(Analysis.created_at.desc()).limit(10).all()
    
    return render_template('dashboard.html',
                           stats=stats,
                           recent_items=recent_items,
                           recent_analyses=recent_analyses)


@blueprint.route("/about")
def about():
    """About page."""
    return render_template('about.html')


# vim: ts=4 sw=4 et
