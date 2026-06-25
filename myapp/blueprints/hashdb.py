"""
Arcology - Hash Database Blueprint

Hash databases, known products, and file recognition.
"""

import csv
import io
import json
import re
from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, or_
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional
from ..database import (
    ANALYSIS_PRIORITY_NORMAL,
    Analysis,
    AnalysisStatus,
    AnalysisType,
    Artefact,
    ExtractedFile,
    HashDatabase,
    HashRescanJob,
    Item,
    KnownFile,
    KnownProduct,
    Partition,
    Platform,
    ProductRecognitionStatus,
    RecognisedProduct,
    RestrictionType,
)
from ..extensions import db
from ..permissions import require_permission
from ..utils.db_helpers import model_choice_list, normalize_hash
from ..utils.web_forms import redirect_local
from ..visibility import artefact_visibility_clause

ROUTENAME = __name__.replace('.', '_')


def _route_redirect(endpoint: str, **values):
    """Redirect to a local HashDB endpoint."""
    return redirect_local(ROUTENAME, endpoint, **values)


def _get_database_or_404(id):
    """Fetch a HashDatabase, treating a soft-deleting one as already gone.

    A database marked is_deleting is being reaped by a background worker job
    and must not be viewed, edited, exported, or re-linked — its rows are
    disappearing underneath any such operation.  It is also hidden from
    listings, so 404 is the consistent "no longer here" response.
    """
    database = db.get_or_404(HashDatabase, id)
    if database.is_deleting:
        abort(404)
    return database


def _view_anchor(db_id: int, anchor: str | None = None):
    """Return the HashDB view URL, optionally with an anchor suffix."""
    url = url_for(f'{ROUTENAME}.view', id=db_id)
    if anchor:
        return url + anchor
    return url


def _prepare_database_form(form: "HashDatabaseForm"):
    """Populate shared choice lists on the HashDB form."""
    form.platform_id.choices = _platform_choices()
    form.restriction_type.choices = _restriction_type_choices()


def _save_database_from_form(database: HashDatabase, form: "HashDatabaseForm"):
    """Copy editable HashDatabase fields from the form to the ORM object."""
    database.name = form.name.data
    database.description = form.description.data
    database.source_url = form.source_url.data
    database.version = form.version.data
    database.platform_id = form.platform_id.data if form.platform_id.data != 0 else None
    database.enable_product_recognition = form.enable_product_recognition.data
    database.exclude_from_similarity = form.exclude_from_similarity.data
    rt_value = form.restriction_type.data
    database.restriction_type = RestrictionType(rt_value) if rt_value else None


def _existing_known_file(database_id: int, product_id: int, md5: str | None, sha1: str | None):
    """Return an existing KnownFile matching the supplied identifying hashes."""
    if md5 and KnownFile.query.filter_by(
        database_id=database_id, product_id=product_id, md5=md5
    ).first():
        return True
    if sha1 and not md5 and KnownFile.query.filter_by(
        database_id=database_id, product_id=product_id, sha1=sha1
    ).first():
        return True
    return False


def _post_known_file_changes(database: HashDatabase, new_kf_list: list[KnownFile]):
    """Queue shared hash-rescan and recognition work after new file imports."""
    if not new_kf_list or not database.is_active:
        return
    from ..services.hash_rescan import queue_hashdb_link_job
    queue_hashdb_link_job(database.id)

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/hashdb', template_folder='templates')


def init_app(app):
    """Register menu items."""
    app.add_menu_item("HashDB", f"{ROUTENAME}.index", 250)


# =============================================================================
# Forms
# =============================================================================

class HashDatabaseForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    source_url = StringField('Source URL', validators=[Optional()])
    version = StringField('Version', validators=[Optional(), Length(max=50)])
    platform_id = SelectField('Platform', coerce=int, validators=[Optional()])
    enable_product_recognition = BooleanField('Folder recognition')
    exclude_from_similarity = BooleanField('Exclude from similarity')
    restriction_type = SelectField('Auto-restrict', coerce=str, validators=[Optional()])


def _restriction_type_choices():
    return [('', '-- None --')] + [
        (rt.value, rt.label) for rt in RestrictionType
    ]


def _platform_choices():
    return model_choice_list(Platform, label='-- All Platforms --')


def _queue_hash_rescan_jobs():
    """Queue HASH_RESCAN Analysis jobs for every artefact with extracted files.

    Skips artefacts that already have a pending or running HASH_RESCAN job
    to avoid flooding the queue on repeated clicks.  Returns the number of
    newly queued jobs.
    """
    artefact_ids_with_files = {
        row[0] for row in
        db.session.query(Partition.artefact_id)
        .filter(Partition.total_files > 0)
        .all()
    }
    if not artefact_ids_with_files:
        return 0

    already_active = {
        row[0] for row in
        db.session.query(Analysis.artefact_id)
        .filter(
            Analysis.artefact_id.in_(artefact_ids_with_files),
            Analysis.analysis_type == AnalysisType.HASH_RESCAN,
            Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
        )
        .all()
    }
    to_queue = artefact_ids_with_files - already_active

    # Clear stale FAILED records before queuing fresh attempts so the status
    # lozenge reflects the new run rather than the old failure.
    if to_queue:
        Analysis.query.filter(
            Analysis.artefact_id.in_(to_queue),
            Analysis.analysis_type == AnalysisType.HASH_RESCAN,
            Analysis.status == AnalysisStatus.FAILED,
        ).delete(synchronize_session=False)

    for aid in to_queue:
        db.session.add(Analysis(
            artefact_id=aid,
            analysis_type=AnalysisType.HASH_RESCAN,
            status=AnalysisStatus.PENDING,
            priority=ANALYSIS_PRIORITY_NORMAL,
        ))
    if to_queue:
        db.session.commit()
    return len(to_queue)


# =============================================================================
# Index / List
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    databases = (
        HashDatabase.query
        .filter(HashDatabase.is_deleting.is_(False))
        .order_by(func.lower(HashDatabase.name))
        .all()
    )
    rescan_job = HashRescanJob.query.order_by(HashRescanJob.id.desc()).first()
    pending_rescan = Analysis.query.filter(
        Analysis.analysis_type == AnalysisType.HASH_RESCAN,
        Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
    ).count()
    return render_template('hashdb/index.html', databases=databases,
                           rescan_job=rescan_job, pending_rescan=pending_rescan)


@blueprint.route('/status.json')
@login_required
def status_json():
    """Return pending hash-rescan count for the page poller."""
    pending = Analysis.query.filter(
        Analysis.analysis_type == AnalysisType.HASH_RESCAN,
        Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
    ).count()
    return jsonify(pending=pending, running=0)


# =============================================================================
# Create
# =============================================================================

@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def new():
    form = HashDatabaseForm()
    _prepare_database_form(form)

    if form.validate_on_submit():
        database = HashDatabase()
        _save_database_from_form(database, form)
        db.session.add(database)
        db.session.commit()
        flash(f'Hash database "{database.name}" created.', 'success')
        return _route_redirect('view', id=database.id)

    return render_template('hashdb/new.html', form=form)


# =============================================================================
# View / Edit / Delete
# =============================================================================

@blueprint.route('/<int:id>')
@login_required
def view(id):
    from ..utils.pagination import (
        VALID_PER_PAGE,
        compute_letter_pages,
        resolve_per_page,
    )

    database = _get_database_or_404(id)

    # Products are paginated (a NIST-scale database has hundreds of thousands):
    # only the current page's rows are loaded, and the per-product file tables
    # are fetched lazily on expand via product_files().
    per_page, page, view_all = resolve_per_page('ITEMS_PER_PAGE', 25)
    products_query = (
        KnownProduct.query
        .filter_by(database_id=id)
        .order_by(func.lower(KnownProduct.title))
    )
    products_pagination = products_query.paginate(
        page=page, per_page=per_page, max_per_page=per_page)
    products = products_pagination.items
    page_product_ids = [p.id for p in products]

    # A-Z jump bar (skip the extra grouping query when it all fits on one page).
    if products_pagination.pages > 1:
        letter_pages, current_letter = compute_letter_pages(
            products_query, func.lower(KnownProduct.title),
            per_page, current_page=products_pagination.page)
    else:
        letter_pages, current_letter = {}, ''

    # Preserve per_page across page/letter navigation.
    pagination_args = {}
    if request.args.get('per_page'):
        pagination_args['per_page'] = request.args.get('per_page')

    platforms = Platform.query.order_by(func.lower(Platform.name)).all()
    rescan_job = (
        HashRescanJob.query
        .filter(or_(HashRescanJob.database_id == id, HashRescanJob.database_id.is_(None)))
        .order_by(HashRescanJob.id.desc())
        .first()
    )
    # Seed for the status poller (only polled while a rescan is running).
    pending_rescan = Analysis.query.filter(
        Analysis.analysis_type == AnalysisType.HASH_RESCAN,
        Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
    ).count()

    # Per-product known-file counts and which products lack a mandatory file
    # (those are ignored by the matcher) — scoped to the current page.
    file_counts = {}
    no_mandatory_ids = set()
    if page_product_ids:
        file_counts = dict(
            db.session.query(KnownFile.product_id, func.count(KnownFile.id))
            .filter(KnownFile.product_id.in_(page_product_ids))
            .group_by(KnownFile.product_id)
            .all()
        )
        with_mandatory = {
            row[0] for row in
            db.session.query(KnownFile.product_id)
            .filter(KnownFile.product_id.in_(page_product_ids),
                    KnownFile.is_required == True)  # noqa: E712
            .distinct()
            .all()
        }
        no_mandatory_ids = set(page_product_ids) - with_mandatory

    # Per-product recognition counts (page only).  These are true product-level
    # matches (all required files found), from the derived recognition table.
    product_recognition_counts = {}
    if (
            page_product_ids and
            database.enable_product_recognition and
            database.product_recognition_status == ProductRecognitionStatus.COMPLETED):
        product_recognition_counts = dict(
            db.session.query(RecognisedProduct.product_id, func.count(RecognisedProduct.id))
            .select_from(RecognisedProduct)
            .join(Partition, RecognisedProduct.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .join(KnownProduct, RecognisedProduct.product_id == KnownProduct.id)
            .filter(KnownProduct.database_id == id)
            .filter(RecognisedProduct.product_id.in_(page_product_ids))
            .filter(artefact_visibility_clause(current_user))
            .group_by(RecognisedProduct.product_id)
            .all()
        )

    return render_template('hashdb/view.html',
                           database=database,
                           products=products,
                           products_pagination=products_pagination,
                           letter_pages=letter_pages,
                           current_letter=current_letter,
                           pagination_args=pagination_args,
                           valid_per_page=VALID_PER_PAGE,
                           view_all=view_all,
                           platforms=platforms,
                           RestrictionType=RestrictionType,
                           rescan_job=rescan_job,
                           pending_rescan=pending_rescan,
                           file_counts=file_counts,
                           no_mandatory_ids=no_mandatory_ids,
                           product_recognition_counts=product_recognition_counts,
                           ProductRecognitionStatus=ProductRecognitionStatus)


@blueprint.route('/<int:id>/products/<int:pid>/files')
@login_required
def product_files(id, pid):
    """Render one product's known-file table.

    Fetched lazily by the view page when a product row is expanded, so the
    bytes for a product's (potentially huge) file list are only produced on
    demand rather than for every product on initial page load.
    """
    _get_database_or_404(id)
    product = KnownProduct.query.filter_by(id=pid, database_id=id).first_or_404()
    files = (
        KnownFile.query
        .filter_by(product_id=pid)
        .order_by(func.lower(KnownFile.filename))
        .all()
    )

    # Per-file match counts, visibility-filtered exactly like view()/search()
    # so the count cannot reveal a known file inside a private artefact.
    # Filtering by product_id (indexed) avoids a giant IN(...) of file ids.
    match_counts = dict(
        db.session.query(ExtractedFile.known_file_id, func.count(ExtractedFile.id))
        .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(KnownFile.product_id == pid)
        .filter(artefact_visibility_clause(current_user))
        .group_by(ExtractedFile.known_file_id)
        .all()
    )

    return render_template('hashdb/_product_files.html',
                           product=product,
                           files=files,
                           match_counts=match_counts)


SEARCH_LIMIT = 200


@blueprint.route('/<int:id>/search')
@login_required
def search(id):
    """Search the collection for artefacts containing files from this database."""
    database = _get_database_or_404(id)

    product_id = request.args.get('product_id', type=int)
    file_id = request.args.get('file_id', type=int)

    # Determine scope
    product = None
    known_file = None
    if file_id:
        known_file = KnownFile.query.filter_by(id=file_id, database_id=id).first_or_404()
        product = known_file.product
        kf_filter = ExtractedFile.known_file_id == file_id
    elif product_id:
        product = KnownProduct.query.filter_by(id=product_id, database_id=id).first_or_404()
        kf_ids = [kf.id for kf in product.known_files]
        if not kf_ids:
            return render_template('hashdb/search.html',
                                   database=database, product=product,
                                   known_file=None, results=[],
                                   truncated=False, unique_items=0,
                                   unique_artefacts=0, SEARCH_LIMIT=SEARCH_LIMIT)
        kf_filter = ExtractedFile.known_file_id.in_(kf_ids)
    else:
        # Whole database — subquery for efficiency
        kf_ids_sq = (
            db.session.query(KnownFile.id)
            .filter(KnownFile.database_id == id)
            .scalar_subquery()
        )
        kf_filter = ExtractedFile.known_file_id.in_(kf_ids_sq)

    if file_id:
        order_by_cols = [func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)]
    elif product_id:
        order_by_cols = [func.lower(KnownFile.filename), func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)]
    else:
        order_by_cols = [func.lower(KnownProduct.title), func.lower(KnownFile.filename), func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)]

    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item, KnownFile, KnownProduct)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
        .outerjoin(KnownProduct, KnownFile.product_id == KnownProduct.id)
        .filter(kf_filter)
        # Hide matches inside artefacts/items the caller may not view, so the
        # hash search cannot be used to enumerate private collections.
        .filter(artefact_visibility_clause(current_user))
        .filter(ExtractedFile.is_directory == False)
        .order_by(*order_by_cols)
        .limit(SEARCH_LIMIT + 1)
        .all()
    )

    truncated = len(q) > SEARCH_LIMIT
    if truncated:
        q = q[:SEARCH_LIMIT]

    unique_items = len({item.id for _, _, _, item, _, _ in q})
    unique_artefacts = len({art.id for _, _, art, _, _, _ in q})

    return render_template('hashdb/search.html',
                           database=database,
                           product=product,
                           known_file=known_file,
                           results=q,
                           truncated=truncated,
                           unique_items=unique_items,
                           unique_artefacts=unique_artefacts,
                           SEARCH_LIMIT=SEARCH_LIMIT)


@blueprint.route('/<int:id>/<int:pid>')
@login_required
def view_product(id, pid):
    database = _get_database_or_404(id)
    product = KnownProduct.query.filter_by(id=pid, database_id=id).first_or_404()

    kf_ids = [kf.id for kf in product.known_files]
    match_counts = {}
    if kf_ids:
        # Visibility-filtered like view()/search(): a count must not reveal that
        # a known file exists inside a private artefact the caller cannot see.
        rows = (
            db.session.query(ExtractedFile.known_file_id, func.count(ExtractedFile.id))
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .filter(ExtractedFile.known_file_id.in_(kf_ids))
            .filter(artefact_visibility_clause(current_user))
            .group_by(ExtractedFile.known_file_id)
            .all()
        )
        match_counts = {kf_id: cnt for kf_id, cnt in rows}

    return render_template('hashdb/edit_product.html',
                           database=database,
                           product=product,
                           match_counts=match_counts)


@blueprint.route('/<int:id>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit(id):
    database = _get_database_or_404(id)
    form = HashDatabaseForm(obj=database)
    _prepare_database_form(form)
    if form.validate_on_submit():
        was_enabled = database.enable_product_recognition
        was_excluded = database.exclude_from_similarity
        _save_database_from_form(database, form)
        db.session.commit()
        flash('Hash database updated.', 'success')
        if database.exclude_from_similarity != was_excluded:
            flash('Similarity exclusion changed — the similarity cache will be '
                  'updated on the next rebuild.', 'info')
        if database.enable_product_recognition and not was_enabled:
            from ..services.hash_rescan import queue_hashdb_recognition_backfill
            _, queued = queue_hashdb_recognition_backfill(database)
            if queued:
                flash('Queued product recognition backfill.', 'info')
        elif was_enabled and not database.enable_product_recognition:
            from ..services.hash_rescan import clear_hashdb_recognition
            clear_hashdb_recognition(database)
            db.session.commit()
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    return _route_redirect('view', id=id)


def _cancel_pending_hashdb_jobs(db_id):
    """Cancel a database's own queued relink/recognition backfill jobs.

    Run before deleting the database so a worker doesn't start (or continue)
    writing to its known_files/extracted_files while we delete them — a
    concurrent writer in the opposite lock order is what produced the
    "deadlock detected" abort mid-delete.  Only PENDING jobs are removed; a
    job already claimed (RUNNING) is left to the deadlock retry below and the
    step's own statement timeout.
    """
    hints_json = json.dumps({'database_id': db_id}, sort_keys=True)
    Analysis.query.filter(
        Analysis.artefact_id.is_(None),
        Analysis.analysis_type.in_([AnalysisType.HASHDB_LINK,
                                    AnalysisType.HASHDB_RECOGNITION]),
        Analysis.hints == hints_json,
        Analysis.status == AnalysisStatus.PENDING,
    ).delete(synchronize_session=False)
    db.session.commit()


@blueprint.route('/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete(id):
    from ..services.hash_rescan import queue_hashdb_delete_job
    database = db.get_or_404(HashDatabase, id)
    name = database.name

    if database.is_deleting:
        # Already scheduled — just make sure the reap job is queued (a no-op if
        # one is already pending/running) and report the in-progress state.
        queue_hashdb_delete_job(id)
        flash(f'Hash database "{name}" is already being deleted in the '
              f'background.', 'info')
        return _route_redirect('index')

    # Deleting a large database (hundreds of thousands of known_files, plus a
    # collection-wide unlink of extracted_files and delete of recognised_products)
    # is far too slow to run inline — it hangs the web thread and deadlock-races
    # the worker.  Instead soft-delete the row and hand the heavy work to a
    # worker-driven bounded-step HASHDB_DELETE job.  Setting is_active=False at
    # the same time removes the half-deleted database from all matching /
    # restriction / recognition queries (which already gate on is_active) for
    # free; is_deleting hides it from listings and blocks management routes.
    _cancel_pending_hashdb_jobs(id)
    database.is_deleting = True
    database.is_active = False
    db.session.commit()

    queue_hashdb_delete_job(id)

    flash(f'Hash database "{name}" is being deleted in the background. '
          f'Affected files will be re-linked against other databases '
          f'automatically.', 'success')
    return _route_redirect('index')


# =============================================================================
# Toggle folder recognition (one-click)
# =============================================================================

@blueprint.route('/<int:id>/toggle-recognition', methods=['POST'])
@login_required
@require_permission('read_write')
def toggle_recognition(id):
    database = _get_database_or_404(id)
    database.enable_product_recognition = not database.enable_product_recognition
    state = 'enabled' if database.enable_product_recognition else 'disabled'
    if database.enable_product_recognition:
        from ..services.hash_rescan import queue_hashdb_recognition_backfill
        _, queued = queue_hashdb_recognition_backfill(database)
        flash(f'Folder recognition {state} for "{database.name}".', 'success')
        if queued:
            flash('Queued product recognition backfill.', 'info')
    else:
        from ..services.hash_rescan import clear_hashdb_recognition
        clear_hashdb_recognition(database)
        db.session.commit()
        flash(f'Folder recognition {state} for "{database.name}".', 'success')
    return _route_redirect('view', id=id)


@blueprint.route('/<int:id>/path-matching/<state>', methods=['POST'])
@login_required
@require_permission('read_write')
def bulk_path_matching(id, state):
    database = _get_database_or_404(id)
    if state not in ('enable', 'disable'):
        flash('Unknown path matching action.', 'danger')
        return _route_redirect('view', id=id)

    enabled = state == 'enable'
    KnownProduct.query.filter_by(database_id=database.id).update(
        {'path_match_enabled': enabled},
        synchronize_session=False,
    )

    queued = False
    if database.enable_product_recognition:
        from ..services.hash_rescan import queue_hashdb_recognition_backfill
        _, queued = queue_hashdb_recognition_backfill(database)
    else:
        db.session.commit()

    label = 'enabled' if enabled else 'disabled'
    flash(f'Path matching {label} for all products in "{database.name}".', 'success')
    if queued:
        flash('Queued product recognition backfill.', 'info')
    return _route_redirect('view', id=id)


@blueprint.route('/<int:id>/toggle-active', methods=['POST'])
@login_required
@require_permission('read_write')
def toggle_active(id):
    database = _get_database_or_404(id)
    database.is_active = not database.is_active
    db.session.commit()
    state = 'enabled' if database.is_active else 'disabled'
    flash(
        f'Hash database "{database.name}" {state}. '
        f'Run "Rescan Known Files" on affected artefacts to update file links.',
        'success' if database.is_active else 'warning',
    )
    return _route_redirect('view', id=id)


# =============================================================================
# Export
# =============================================================================

# Leading characters that make Excel / LibreOffice interpret a CSV cell as a
# formula.  Known-product fields (title, filename, description, path) are
# free-text supplied by read_write users, so a value like ``=cmd|'/c calc'!A1``
# would execute when another user opens the exported CSV (CWE-1236).
_CSV_FORMULA_TRIGGERS = ('=', '+', '-', '@', '\t', '\r')


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in a CSV cell.

    Prefixes a single quote to any string whose first character would otherwise
    trigger formula evaluation, so the value is read as literal text.  Numbers,
    hashes and booleans (which never start with a trigger character) pass
    through unchanged.
    """
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def _safe_download_name(name: str, suffix: str) -> str:
    """Build a Content-Disposition filename safe from header/quote injection."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('_')
    return f'{cleaned or "hashdb"}{suffix}'


@blueprint.route('/<int:id>/export')
@login_required
def export(id):
    database = _get_database_or_404(id)
    fmt = request.args.get('format', 'json').lower()
    products = KnownProduct.query.filter_by(database_id=id).order_by(func.lower(KnownProduct.title)).all()

    if fmt == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['product_title', 'filename', 'file_size', 'md5', 'sha1', 'sha256',
                         'crc32', 'is_required', 'relative_path', 'description'])
        for product in products:
            for kf in product.known_files:
                writer.writerow([_csv_safe(c) for c in (
                    product.title, kf.filename, kf.file_size or '',
                    kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                    '1' if kf.is_required else '0',
                    kf.relative_path or '', kf.description or '',
                )])
        for kf in KnownFile.query.filter_by(database_id=id, product_id=None).all():
            writer.writerow([_csv_safe(c) for c in (
                '', kf.filename, kf.file_size or '',
                kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                '1' if kf.is_required else '0',
                kf.relative_path or '', kf.description or '',
            )])
        filename = _safe_download_name(database.name, '.csv')
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    # JSON
    data = {
        'schema_version': 1,
        'database': {
            'name': database.name,
            'description': database.description,
            'version': database.version,
            'source_url': database.source_url,
            'enable_product_recognition': database.enable_product_recognition,
        },
        'products': [
            {
                'title': p.title,
                'description': p.description,
                'path_match_enabled': p.path_match_enabled,
                'files': [
                    {
                        'filename': kf.filename,
                        'file_size': kf.file_size,
                        'md5': kf.md5,
                        'sha1': kf.sha1,
                        'sha256': kf.sha256,
                        'crc32': kf.crc32,
                        'is_required': kf.is_required,
                        'relative_path': kf.relative_path,
                        'description': kf.description,
                    }
                    for kf in p.known_files
                ],
            }
            for p in products
        ],
    }
    filename = _safe_download_name(database.name, '.json')
    return Response(json.dumps(data, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# =============================================================================
# Import
# =============================================================================

@blueprint.route('/import', methods=['POST'])
@login_required
@require_permission('read_write')
def import_database():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file uploaded.', 'danger')
        return _route_redirect('index')

    name_override = request.form.get('name', '').strip()
    merge = 'merge' in request.form

    filename_lower = f.filename.lower()
    if filename_lower.endswith('.json'):
        fmt = 'json'
    elif filename_lower.endswith('.csv'):
        fmt = 'csv'
    else:
        flash('Unknown format — use a .json or .csv file.', 'danger')
        return _route_redirect('index')

    try:
        content = f.read().decode('utf-8')
    except Exception as e:
        flash(f'Could not read file: {e}', 'danger')
        return _route_redirect('index')

    if fmt == 'json':
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            flash(f'Invalid JSON: {e}', 'danger')
            return _route_redirect('index')

        db_info = data.get('database', {})
        db_name = name_override or db_info.get('name', '').strip()
        if not db_name:
            flash('The JSON file has no database name; provide one in the Name field.', 'danger')
            return _route_redirect('index')

        database = HashDatabase.query.filter_by(name=db_name).first()
        if database and database.is_deleting:
            flash(f'"{db_name}" is currently being deleted in the background. '
                  f'Wait for that to finish before importing under the same name.',
                  'danger')
            return _route_redirect('index')
        if database and not merge:
            flash(f'"{db_name}" already exists. Tick "Merge into existing" to add to it.', 'danger')
            return _route_redirect('index')
        if not database:
            database = HashDatabase(
                name=db_name,
                description=db_info.get('description'),
                version=db_info.get('version'),
                source_url=db_info.get('source_url'),
                enable_product_recognition=db_info.get('enable_product_recognition', False),
            )
            db.session.add(database)
            db.session.flush()

        products_added = files_added = 0
        new_kf_list = []
        for p_data in data.get('products', []):
            p_title = (p_data.get('title') or '').strip()
            if not p_title:
                continue
            product = KnownProduct.query.filter_by(database_id=database.id, title=p_title).first()
            if not product:
                product = KnownProduct(
                    database_id=database.id,
                    title=p_title,
                    description=p_data.get('description'),
                    path_match_enabled=p_data.get('path_match_enabled', False),
                )
                db.session.add(product)
                db.session.flush()
                products_added += 1
            for f_data in p_data.get('files', []):
                md5 = normalize_hash(f_data.get('md5'))
                sha1_raw = normalize_hash(f_data.get('sha1'))
                if _existing_known_file(database.id, product.id, md5, sha1_raw):
                    continue
                kf = KnownFile(
                    database_id=database.id,
                    product_id=product.id,
                    filename=f_data.get('filename', ''),
                    file_size=f_data.get('file_size'),
                    md5=md5,
                    sha1=sha1_raw,
                    sha256=normalize_hash(f_data.get('sha256')),
                    crc32=normalize_hash(f_data.get('crc32')),
                    is_required=f_data.get('is_required', True),
                    relative_path=f_data.get('relative_path') or None,
                    description=f_data.get('description') or None,
                )
                db.session.add(kf)
                new_kf_list.append(kf)
                files_added += 1
        db.session.commit()
        flash(f'Imported {products_added} product(s) and {files_added} file(s) into "{database.name}".', 'success')
        _post_known_file_changes(database, new_kf_list)
        return _route_redirect('view', id=database.id)

    else:  # CSV
        db_name = name_override
        if not db_name:
            flash('A database name is required for CSV import.', 'danger')
            return _route_redirect('index')

        database = HashDatabase.query.filter_by(name=db_name).first()
        if database and database.is_deleting:
            flash(f'"{db_name}" is currently being deleted in the background. '
                  f'Wait for that to finish before importing under the same name.',
                  'danger')
            return _route_redirect('index')
        if database and not merge:
            flash(f'"{db_name}" already exists. Tick "Merge into existing" to add to it.', 'danger')
            return _route_redirect('index')
        if not database:
            database = HashDatabase(name=db_name)
            db.session.add(database)
            db.session.flush()

        reader = csv.DictReader(io.StringIO(content))
        product_cache: dict[str, KnownProduct] = {}
        files_added = 0
        new_kf_list = []
        for row in reader:
            p_title = (row.get('product_title') or '').strip()
            if not p_title:
                continue
            if p_title not in product_cache:
                product = KnownProduct.query.filter_by(database_id=database.id, title=p_title).first()
                if not product:
                    product = KnownProduct(database_id=database.id, title=p_title)
                    db.session.add(product)
                    db.session.flush()
                product_cache[p_title] = product
            product = product_cache[p_title]

            md5 = normalize_hash(row.get('md5'))
            sha1 = normalize_hash(row.get('sha1'))
            if _existing_known_file(database.id, product.id, md5, sha1):
                continue

            file_size_str = (row.get('file_size') or '').strip()
            try:
                file_size = int(file_size_str) if file_size_str else None
            except ValueError:
                file_size = None

            kf = KnownFile(
                database_id=database.id,
                product_id=product.id,
                filename=(row.get('filename') or '').strip(),
                file_size=file_size,
                md5=md5,
                sha1=sha1,
                sha256=normalize_hash(row.get('sha256')),
                crc32=normalize_hash(row.get('crc32')),
                is_required=((row.get('is_required') or '1').strip() == '1'),
                relative_path=(row.get('relative_path') or '').strip() or None,
                description=(row.get('description') or '').strip() or None,
            )
            db.session.add(kf)
            new_kf_list.append(kf)
            files_added += 1

        db.session.commit()
        flash(f'Imported {files_added} file(s) from CSV into "{database.name}".', 'success')
        _post_known_file_changes(database, new_kf_list)
        return _route_redirect('view', id=database.id)


@blueprint.route('/rescan', methods=['POST'])
@login_required
@require_permission('read_write')
def rescan_all():
    """Queue HASH_RESCAN worker jobs for all artefacts (from the index page)."""
    n = _queue_hash_rescan_jobs()
    if n:
        flash(f'Hash rescan queued: {n} artefact(s) will be processed by the worker.', 'info')
    else:
        flash('No artefacts with extracted files found, or all are already queued.', 'warning')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<int:id>/rescan', methods=['POST'])
@login_required
@require_permission('read_write')
def rescan(id):
    """Relink the collection against this one database (single worker job).

    Scoped to *id*: queues one HASHDB_LINK analysis (the same bounded-step
    relink the import path uses) rather than fanning out a per-artefact
    HASH_RESCAN across the whole collection against every database — that
    enqueues thousands of rows synchronously and is not specific to this DB.
    A recognition backfill is queued automatically by the link job when it
    finishes (if this database has product recognition enabled).
    """
    from ..services.hash_rescan import queue_hashdb_link_job
    database = _get_database_or_404(id)
    if not database.is_active:
        flash('This database is inactive, so it is excluded from hash linking. '
              'Activate it first to relink artefacts.', 'warning')
        return redirect(url_for(f'{ROUTENAME}.view', id=id))
    _, queued = queue_hashdb_link_job(database.id)
    if queued:
        flash('Relink queued: extracted files will be matched against this '
              'database by the worker.', 'info')
    else:
        flash('A relink for this database is already queued or running.', 'warning')
    return redirect(url_for(f'{ROUTENAME}.view', id=id))


# =============================================================================
# Known Products
# =============================================================================

@blueprint.route('/<int:db_id>/products/new', methods=['POST'])
@login_required
@require_permission('read_write')
def new_known_product(db_id):
    _get_database_or_404(db_id)
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return _route_redirect('view', id=db_id)
    product = KnownProduct(
        database_id=db_id,
        title=title,
        description=request.form.get('description', '').strip() or None,
        path_match_enabled='path_match_enabled' in request.form,
    )
    db.session.add(product)
    db.session.commit()
    flash(f'Product "{product.title}" added.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=product.id))


@blueprint.route('/<int:db_id>/products/<int:pid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = request.form.get('title', '').strip()
    if not title:
        flash('Product title is required.', 'danger')
        return _route_redirect('view', id=db_id)
    product.title = title
    product.description = request.form.get('description', '').strip() or None
    product.path_match_enabled = 'path_match_enabled' in request.form
    db.session.commit()
    flash(f'Product "{product.title}" updated.', 'success')
    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


@blueprint.route('/<int:db_id>/products/<int:pid>/files/save-all', methods=['POST'])
@login_required
@require_permission('read_write')
def save_all_files(db_id, pid):
    """Save edits to all files in a product in one batched submission.

    Only files whose hash/size/metadata actually changed are rescanned.
    Rejects the whole submission if any file would end up with no hashes.
    """
    from ..services.hash_rescan import (
        queue_hashdb_recognition_backfill,
        rescan_hashes_for_known_file,
        rescan_links_for_known_file_id,
    )

    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    is_active = product.database.is_active
    enable_recognition = product.database.enable_product_recognition

    # Collect the proposed new values for each file, validate up front so we
    # don't commit a partially-applied batch.
    proposed = {}
    for kf in product.known_files:
        filename = request.form.get(f'filename_{kf.id}', '').strip()
        if not filename:
            flash(f'Filename for file #{kf.id} cannot be empty.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))
        md5 = normalize_hash(request.form.get(f'md5_{kf.id}'))
        sha1 = normalize_hash(request.form.get(f'sha1_{kf.id}'))
        sha256 = normalize_hash(request.form.get(f'sha256_{kf.id}'))
        crc32 = normalize_hash(request.form.get(f'crc32_{kf.id}'))
        if not any([md5, sha1, sha256]):
            flash(f'File "{filename}" must have at least one of MD5, SHA1, or SHA256.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))
        size_str = request.form.get(f'file_size_{kf.id}', '').strip()
        proposed[kf.id] = {
            'filename': filename,
            'md5': md5, 'sha1': sha1, 'sha256': sha256, 'crc32': crc32,
            'file_size': int(size_str) if size_str.isdigit() else None,
            'relative_path': request.form.get(f'relative_path_{kf.id}', '').strip() or None,
            'is_required': f'is_required_{kf.id}' in request.form,
        }

    # Apply changes, tracking which files actually changed so we only rescan
    # those.  Changes to hash/size are what affect rescan outcomes; other
    # fields are metadata-only.
    RESCAN_FIELDS = ('md5', 'sha1', 'sha256', 'file_size')
    changed_for_rescan = []
    any_changed = False
    for kf in product.known_files:
        new = proposed[kf.id]
        if any(getattr(kf, f) != new[f] for f in RESCAN_FIELDS):
            changed_for_rescan.append(kf)
        if any(getattr(kf, f) != new[f] for f in new):
            any_changed = True
        for field, value in new.items():
            setattr(kf, field, value)

    db.session.commit()
    if any_changed:
        flash('Files updated.', 'success')
    else:
        flash('No changes.', 'info')

    if is_active and changed_for_rescan:
        for kf in changed_for_rescan:
            rescan_links_for_known_file_id(kf.id)
            rescan_hashes_for_known_file(kf)
        if enable_recognition:
            queue_hashdb_recognition_backfill(product.database)

    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


@blueprint.route('/<int:db_id>/products/<int:pid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_known_product(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    title = product.title
    database = product.database
    is_active = database.is_active
    enable_recognition = database.enable_product_recognition

    # A product's own known_files list is small, but a ubiquitous application
    # directory (!System, !Scrap, !Fonts, …) can be recognised in thousands of
    # partitions.  Deleting via the ORM (db.session.delete(product)) would load
    # every recognised_products row through the recognised_in delete-orphan
    # cascade and DELETE them one at a time — the same pathological aggregate as
    # the whole-database delete (issue #618).  Do the work as bulk statements.
    kf_id_query = (
        db.session.query(KnownFile.id)
        .filter(KnownFile.product_id == pid)
    )

    # Collect affected ExtractedFile IDs before unlinking.
    affected_ef_ids = [
        row[0] for row in
        ExtractedFile.query
        .with_entities(ExtractedFile.id)
        .filter(ExtractedFile.known_file_id.in_(kf_id_query))
        .all()
    ]
    if affected_ef_ids:
        # Clear FK references so the delete cannot violate the constraint.
        ExtractedFile.query.filter(
            ExtractedFile.id.in_(affected_ef_ids)
        ).update({'known_file_id': None}, synchronize_session=False)

    # Bulk-delete in FK-safe order: recognised_products (referenced this
    # product) and known_files, then the product row.  Deleting
    # recognised_products explicitly keeps it correct regardless of whether the
    # backend enforces the ON DELETE CASCADE FK (see delete() above).
    RecognisedProduct.query.filter(
        RecognisedProduct.product_id == pid
    ).delete(synchronize_session=False)
    KnownFile.query.filter(
        KnownFile.product_id == pid
    ).delete(synchronize_session=False)
    KnownProduct.query.filter(
        KnownProduct.id == pid
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f'Product "{title}" deleted.', 'success')

    if is_active and affected_ef_ids:
        from ..services.hash_rescan import queue_hashdb_recognition_backfill, rescan_hashes_for_queryset
        # Re-evaluate the unlinked files; they may match another active database.
        rescan_hashes_for_queryset(ExtractedFile.query.filter(ExtractedFile.id.in_(affected_ef_ids)))
        if enable_recognition:
            queue_hashdb_recognition_backfill(database)

    return _route_redirect('view', id=db_id)


# =============================================================================
# Known Files (within products)
# =============================================================================

@blueprint.route('/<int:db_id>/products/<int:pid>/files/add', methods=['POST'])
@login_required
@require_permission('read_write')
def add_known_file(db_id, pid):
    product = KnownProduct.query.filter_by(id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))
    md5 = normalize_hash(request.form.get('md5'))
    sha1 = normalize_hash(request.form.get('sha1'))
    sha256 = normalize_hash(request.form.get('sha256'))
    crc32 = normalize_hash(request.form.get('crc32'))
    if not any([md5, sha1, sha256]):
        flash('At least one of MD5, SHA1, or SHA256 is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))
    file_size_str = request.form.get('file_size', '').strip()
    file_size = int(file_size_str) if file_size_str.isdigit() else None
    kf = KnownFile(
        database_id=db_id,
        product_id=pid,
        filename=filename,
        file_size=file_size,
        md5=md5,
        sha1=sha1,
        sha256=sha256,
        crc32=crc32,
        is_required='is_required' in request.form,
        relative_path=request.form.get('relative_path', '').strip() or None,
        description=request.form.get('description', '').strip() or None,
    )
    db.session.add(kf)
    db.session.commit()
    flash(f'File "{filename}" added to "{product.title}".', 'success')
    if product.database.is_active:
        from ..services.hash_rescan import queue_hashdb_recognition_backfill, rescan_hashes_for_known_file
        rescan_hashes_for_known_file(kf)
        if product.database.enable_product_recognition:
            queue_hashdb_recognition_backfill(product.database)
    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


@blueprint.route('/<int:db_id>/products/<int:pid>/files/<int:fid>/edit', methods=['POST'])
@login_required
@require_permission('read_write')
def edit_known_file(db_id, pid, fid):
    kf = KnownFile.query.filter_by(id=fid, product_id=pid, database_id=db_id).first_or_404()
    filename = request.form.get('filename', '').strip()
    if not filename:
        flash('Filename is required.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))
    kf_id = kf.id
    is_active = kf.database.is_active
    enable_recognition = kf.database.enable_product_recognition
    kf.filename = filename
    kf.md5 = normalize_hash(request.form.get('md5'))
    kf.sha1 = normalize_hash(request.form.get('sha1'))
    kf.sha256 = normalize_hash(request.form.get('sha256'))
    kf.crc32 = normalize_hash(request.form.get('crc32'))
    file_size_str = request.form.get('file_size', '').strip()
    kf.file_size = int(file_size_str) if file_size_str.isdigit() else None
    kf.is_required = 'is_required' in request.form
    kf.relative_path = request.form.get('relative_path', '').strip() or None
    kf.description = request.form.get('description', '').strip() or None
    db.session.commit()
    flash(f'File "{kf.filename}" updated.', 'success')
    if is_active:
        from ..services.hash_rescan import (
            queue_hashdb_recognition_backfill,
            rescan_hashes_for_known_file,
            rescan_links_for_known_file_id,
        )
        # Re-evaluate files that were linked via the old hashes (they may
        # no longer match), then scan for files matching the new hashes.
        rescan_links_for_known_file_id(kf_id)
        rescan_hashes_for_known_file(kf)
        if enable_recognition:
            queue_hashdb_recognition_backfill(kf.database)
    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


@blueprint.route('/<int:db_id>/products/<int:pid>/files/<int:fid>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete_known_file(db_id, pid, fid):
    kf = KnownFile.query.filter_by(id=fid, product_id=pid, database_id=db_id).first_or_404()
    filename = kf.filename
    kf_id = kf.id
    database = kf.database
    is_active = database.is_active
    enable_recognition = database.enable_product_recognition

    # Collect affected ExtractedFile IDs before unlinking so we can rescan
    # them afterwards.  Must be done before the delete to avoid FK violation.
    affected_ef_ids = [
        row[0] for row in
        ExtractedFile.query
        .with_entities(ExtractedFile.id)
        .filter(ExtractedFile.known_file_id == kf_id)
        .all()
    ]
    # Clear FK references before deletion to avoid constraint violation.
    if affected_ef_ids:
        ExtractedFile.query.filter(
            ExtractedFile.id.in_(affected_ef_ids)
        ).update({'known_file_id': None}, synchronize_session=False)

    db.session.delete(kf)
    db.session.commit()
    flash(f'File "{filename}" deleted.', 'success')

    if is_active and affected_ef_ids:
        from ..services.hash_rescan import queue_hashdb_recognition_backfill, rescan_hashes_for_queryset
        # Re-evaluate unlinked files; they may match another active database.
        rescan_hashes_for_queryset(ExtractedFile.query.filter(ExtractedFile.id.in_(affected_ef_ids)))
        if enable_recognition:
            queue_hashdb_recognition_backfill(database)

    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


# vim: ts=4 sw=4 et
