"""
Arcology - Dashboard Blueprint

Homepage and dashboard views.
"""

from flask import Blueprint, jsonify, render_template
from flask_login import current_user
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload
from ..database import Analysis, AnalysisStatus, Artefact, Item
from ..extensions import db
from ..permissions import public_readable
from ..visibility import artefact_visibility_clause, item_visibility_clause

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("Dashboard", f"{ROUTENAME}.index", -1000)


def _get_stats(user):
    """Compute dashboard statistics, scoped to what *user* may see."""
    item_clause = item_visibility_clause(user)
    artefact_clause = artefact_visibility_clause(user)

    total_items = db.session.query(func.count(Item.id)).filter(item_clause).scalar()
    total_artefacts = (
        db.session.query(func.count(Artefact.id))
        .join(Item, Artefact.item_id == Item.id)
        .filter(artefact_clause)
        .scalar()
    )
    analysis_counts = (
        db.session.query(
            func.count(case((Analysis.status == AnalysisStatus.PENDING, 1))).label('pending'),
            func.count(case((Analysis.status == AnalysisStatus.RUNNING, 1))).label('running'),
        )
        .select_from(Analysis)
        .join(Artefact, Analysis.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(artefact_clause)
        .one()
    )

    return {
        'total_items': total_items or 0,
        'total_artefacts': total_artefacts or 0,
        'pending_analyses': analysis_counts.pending or 0,
        'running_analyses': analysis_counts.running or 0,
    }


@blueprint.route("/")
@public_readable
def index():
    """Homepage with dashboard statistics."""
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
        .filter(item_visibility_clause(current_user))
        .order_by(Item.created_at.desc())
        .limit(10)
        .all()
    )

    # Recent analyses with artefact eagerly loaded (avoids N+1)
    recent_analyses = (
        Analysis.query
        .options(joinedload(Analysis.artefact))
        .join(Artefact, Analysis.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(artefact_visibility_clause(current_user))
        .order_by(Analysis.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template('dashboard.html',
                           stats=_get_stats(current_user),
                           recent_items=recent_items,
                           recent_analyses=recent_analyses)


@blueprint.route("/stats.json")
@public_readable
def stats_json():
    """Dashboard statistics as JSON (for live counter updates)."""
    return jsonify(_get_stats(current_user))


@blueprint.route("/about")
def about():
    """About page."""
    return render_template('about.html')


# vim: ts=4 sw=4 et
