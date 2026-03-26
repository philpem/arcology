"""
Arcology - Dashboard Blueprint

Homepage and dashboard views.
"""

from flask import Blueprint, render_template
from flask_login import login_required
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload

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
    # Single query for all four stats using scalar subqueries + conditional counts
    stats_row = db.session.query(
        db.session.query(func.count(Item.id)).scalar_subquery().label('total_items'),
        db.session.query(func.count(Artefact.id)).scalar_subquery().label('total_artefacts'),
        func.count(case((Analysis.status == AnalysisStatus.PENDING, 1))).label('pending'),
        func.count(case((Analysis.status == AnalysisStatus.RUNNING, 1))).label('running'),
    ).select_from(Analysis).one()

    stats = {
        'total_items': stats_row.total_items or 0,
        'total_artefacts': stats_row.total_artefacts or 0,
        'pending_analyses': stats_row.pending or 0,
        'running_analyses': stats_row.running or 0,
    }

    # Recent items with artefact count subquery (avoids N+1 lazy loads)
    artefact_count_sq = (
        db.session.query(func.count(Artefact.id))
        .filter(Artefact.item_id == Item.id)
        .correlate(Item)
        .scalar_subquery()
        .label('artefact_count')
    )
    recent_items = (
        db.session.query(Item, artefact_count_sq)
        .order_by(Item.created_at.desc())
        .limit(10)
        .all()
    )

    # Recent analyses with artefact eagerly loaded (avoids N+1)
    recent_analyses = (
        Analysis.query
        .options(joinedload(Analysis.artefact))
        .order_by(Analysis.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template('dashboard.html',
                           stats=stats,
                           recent_items=recent_items,
                           recent_analyses=recent_analyses)


@blueprint.route("/about")
def about():
    """About page."""
    return render_template('about.html')


# vim: ts=4 sw=4 et
