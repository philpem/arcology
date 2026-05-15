"""
Arcology - Hash Database Blueprint

Hash databases, known products, and file recognition.
"""

import csv
import io
import json
import threading
from datetime import datetime, timezone
from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, or_
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional
from ..database import (
    Artefact,
    ExtractedFile,
    HashDatabase,
    HashRescanJob,
    HashRescanStatus,
    Item,
    KnownFile,
    KnownProduct,
    Partition,
    Platform,
    RestrictionType,
)
from ..extensions import db
from ..permissions import require_permission
from ..utils.db_helpers import model_choice_list, normalize_hash
from ..utils.web_forms import redirect_local

ROUTENAME = __name__.replace('.', '_')


def _partition_ids_for_hashes(md5, sha1, file_size=None):
    """Return a set of partition_ids for ExtractedFiles matching md5 or sha1."""
    conditions = []
    if md5:
        conditions.append(ExtractedFile.md5 == md5)
    if sha1:
        conditions.append(ExtractedFile.sha1 == sha1)
    if not conditions:
        return set()
    query = (
        ExtractedFile.query
        .with_entities(ExtractedFile.partition_id)
        .filter(or_(*conditions))
    )
    if file_size is not None:
        query = query.filter(ExtractedFile.file_size == file_size)
    return {row[0] for row in query.all()}


def _route_redirect(endpoint: str, **values):
    """Redirect to a local HashDB endpoint."""
    return redirect_local(ROUTENAME, endpoint, **values)


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
    """Run shared hash-rescan and recognition queueing after new file imports."""
    if not database.is_active or not new_kf_list:
        return

    from ..utils.hash_rescan import (
        queue_product_recognition_for_partitions,
        rescan_hashes_for_new_known_files,
    )

    rescan_hashes_for_new_known_files(new_kf_list)

    if not database.enable_product_recognition:
        return

    md5s = [kf.md5 for kf in new_kf_list if kf.md5]
    sha1s = [kf.sha1 for kf in new_kf_list if kf.sha1]
    conditions = ([ExtractedFile.md5.in_(md5s)] if md5s else []) + (
        [ExtractedFile.sha1.in_(sha1s)] if sha1s else []
    )
    if not conditions:
        return

    partition_ids = {
        row[0] for row in
        ExtractedFile.query
        .with_entities(ExtractedFile.partition_id)
        .filter(or_(*conditions))
        .all()
    }
    queue_product_recognition_for_partitions(partition_ids)

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
    restriction_type = SelectField('Auto-restrict', coerce=str, validators=[Optional()])


def _restriction_type_choices():
    return [('', '-- None --')] + [
        (rt.value, rt.label) for rt in RestrictionType
    ]


def _platform_choices():
    return model_choice_list(Platform, label='-- All Platforms --')


# =============================================================================
# Index / List
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    databases = HashDatabase.query.order_by(func.lower(HashDatabase.name)).all()
    rescan_job = HashRescanJob.query.order_by(HashRescanJob.id.desc()).first()
    return render_template('hashdb/index.html', databases=databases, rescan_job=rescan_job)


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
    database = HashDatabase.query.get_or_404(id)
    products = (
        KnownProduct.query
        .options(db.joinedload(KnownProduct.known_files))
        .filter_by(database_id=id)
        .order_by(func.lower(KnownProduct.title))
        .all()
    )
    platforms = Platform.query.order_by(func.lower(Platform.name)).all()
    rescan_job = (
        HashRescanJob.query
        .filter(or_(HashRescanJob.database_id == id, HashRescanJob.database_id.is_(None)))
        .order_by(HashRescanJob.id.desc())
        .first()
    )

    kf_ids = [kf.id for p in products for kf in p.known_files]
    match_counts = {}
    if kf_ids:
        rows = (
            db.session.query(ExtractedFile.known_file_id, func.count(ExtractedFile.id))
            .filter(ExtractedFile.known_file_id.in_(kf_ids))
            .group_by(ExtractedFile.known_file_id)
            .all()
        )
        match_counts = {kf_id: cnt for kf_id, cnt in rows}

    product_match_counts = {
        p.id: sum(match_counts.get(kf.id, 0) for kf in p.known_files)
        for p in products
    }

    return render_template('hashdb/view.html',
                           database=database,
                           products=products,
                           platforms=platforms,
                           RestrictionType=RestrictionType,
                           rescan_job=rescan_job,
                           match_counts=match_counts,
                           product_match_counts=product_match_counts)


SEARCH_LIMIT = 200


@blueprint.route('/<int:id>/search')
@login_required
def search(id):
    """Search the collection for artefacts containing files from this database."""
    database = HashDatabase.query.get_or_404(id)

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

    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item, KnownFile)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
        .filter(kf_filter)
        .filter(ExtractedFile.is_directory == False)
        .order_by(func.lower(KnownFile.filename), func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path))
        .limit(SEARCH_LIMIT + 1)
        .all()
    )

    truncated = len(q) > SEARCH_LIMIT
    if truncated:
        q = q[:SEARCH_LIMIT]

    unique_items = len({item.id for _, _, _, item, _ in q})
    unique_artefacts = len({art.id for _, _, art, _, _ in q})

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
    database = HashDatabase.query.get_or_404(id)
    product = KnownProduct.query.filter_by(id=pid, database_id=id).first_or_404()

    kf_ids = [kf.id for kf in product.known_files]
    match_counts = {}
    if kf_ids:
        rows = (
            db.session.query(ExtractedFile.known_file_id, func.count(ExtractedFile.id))
            .filter(ExtractedFile.known_file_id.in_(kf_ids))
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
    database = HashDatabase.query.get_or_404(id)
    form = HashDatabaseForm(obj=database)
    _prepare_database_form(form)
    if form.validate_on_submit():
        _save_database_from_form(database, form)
        db.session.commit()
        flash('Hash database updated.', 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    return _route_redirect('view', id=id)


@blueprint.route('/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('read_write')
def delete(id):
    database = HashDatabase.query.get_or_404(id)
    name = database.name
    is_active = database.is_active

    # Collect all KnownFile IDs that will be cascade-deleted so we can clear
    # ExtractedFile.known_file_id references first (FK has no ON DELETE action).
    kf_ids = [
        kf.id
        for product in database.known_products
        for kf in product.known_files
    ]
    affected_ef_ids = []
    if kf_ids:
        affected_ef_ids = [
            row[0] for row in
            ExtractedFile.query
            .with_entities(ExtractedFile.id)
            .filter(ExtractedFile.known_file_id.in_(kf_ids))
            .all()
        ]
        if affected_ef_ids:
            ExtractedFile.query.filter(
                ExtractedFile.id.in_(affected_ef_ids)
            ).update({'known_file_id': None, 'is_known': False}, synchronize_session=False)

    db.session.delete(database)
    db.session.commit()
    flash(f'Hash database "{name}" deleted.', 'success')

    # Re-evaluate unlinked files so they may link to another active database.
    if is_active and affected_ef_ids:
        from ..utils.hash_rescan import rescan_hashes_for_queryset
        rescan_hashes_for_queryset(ExtractedFile.query.filter(ExtractedFile.id.in_(affected_ef_ids)))

    return _route_redirect('index')


# =============================================================================
# Toggle folder recognition (one-click)
# =============================================================================

@blueprint.route('/<int:id>/toggle-recognition', methods=['POST'])
@login_required
@require_permission('read_write')
def toggle_recognition(id):
    database = HashDatabase.query.get_or_404(id)
    database.enable_product_recognition = not database.enable_product_recognition
    db.session.commit()
    state = 'enabled' if database.enable_product_recognition else 'disabled'
    flash(f'Folder recognition {state} for "{database.name}".', 'success')
    if database.enable_product_recognition:
        # Newly enabled: queue PRODUCT_RECOGNITION for every partition
        # that has extracted files so the worker can produce results.
        from ..database import Partition
        from ..utils.hash_rescan import queue_product_recognition_for_partitions
        partition_ids = {
            row[0] for row in
            Partition.query
            .with_entities(Partition.id)
            .filter(Partition.total_files > 0)
            .all()
        }
        queued = queue_product_recognition_for_partitions(partition_ids)
        if queued:
            flash(f'Queued product recognition for {queued} partition(s).', 'info')
    return _route_redirect('view', id=id)


@blueprint.route('/<int:id>/toggle-active', methods=['POST'])
@login_required
@require_permission('read_write')
def toggle_active(id):
    database = HashDatabase.query.get_or_404(id)
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

@blueprint.route('/<int:id>/export')
@login_required
def export(id):
    database = HashDatabase.query.get_or_404(id)
    fmt = request.args.get('format', 'json').lower()
    products = KnownProduct.query.filter_by(database_id=id).order_by(func.lower(KnownProduct.title)).all()

    if fmt == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['product_title', 'filename', 'file_size', 'md5', 'sha1', 'sha256',
                         'crc32', 'is_required', 'relative_path', 'description'])
        for product in products:
            for kf in product.known_files:
                writer.writerow([
                    product.title, kf.filename, kf.file_size or '',
                    kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                    '1' if kf.is_required else '0',
                    kf.relative_path or '', kf.description or '',
                ])
        for kf in KnownFile.query.filter_by(database_id=id, product_id=None).all():
            writer.writerow([
                '', kf.filename, kf.file_size or '',
                kf.md5 or '', kf.sha1 or '', kf.sha256 or '', kf.crc32 or '',
                '1' if kf.is_required else '0',
                kf.relative_path or '', kf.description or '',
            ])
        filename = f"{database.name.replace(' ', '_')}.csv"
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
    filename = f"{database.name.replace(' ', '_')}.json"
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
        database.file_count = (database.file_count or 0) + files_added
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

        database.file_count = (database.file_count or 0) + files_added
        db.session.commit()
        flash(f'Imported {files_added} file(s) from CSV into "{database.name}".', 'success')
        _post_known_file_changes(database, new_kf_list)
        return _route_redirect('view', id=database.id)


# =============================================================================
# Background rescan
# =============================================================================

def _run_rescan_background(app, job_id):
    """Background thread: run a full hash rescan and update the HashRescanJob row.

    All state is stored in the database so every gunicorn worker process
    sees the same status — no in-process shared state is used.
    """
    from ..utils.hash_rescan import (
        apply_database_restrictions,
        queue_product_recognition_for_partitions,
        rescan_hashes_all,
    )

    with app.app_context():
        job = db.session.get(HashRescanJob, job_id)
        if not job:
            return
        job.started_at = datetime.now(timezone.utc)
        db.session.commit()

        try:
            updated, total = rescan_hashes_all()

            # Apply auto-restrictions from flagged databases to artefacts
            # whose extracted files now match those databases.
            has_flagged_db = HashDatabase.query.filter(
                HashDatabase.is_active == True,
                HashDatabase.restriction_type.isnot(None),
            ).first()
            if has_flagged_db:
                affected_artefacts = (
                    Artefact.query
                    .join(Partition, Partition.artefact_id == Artefact.id)
                    .join(ExtractedFile, ExtractedFile.partition_id == Partition.id)
                    .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
                    .join(HashDatabase, KnownFile.database_id == HashDatabase.id)
                    .filter(
                        ExtractedFile.is_known == True,
                        HashDatabase.restriction_type.isnot(None),
                    )
                    .distinct()
                    .all()
                )
                for artefact in affected_artefacts:
                    apply_database_restrictions(artefact)

            # Queue product recognition for all partitions that have files,
            # but only for databases that have it enabled.
            queued = 0
            has_recognition = HashDatabase.query.filter_by(
                is_active=True, enable_product_recognition=True
            ).first()
            if has_recognition:
                partition_ids = {
                    row[0] for row in
                    Partition.query
                    .with_entities(Partition.id)
                    .filter(Partition.total_files > 0)
                    .all()
                }
                queued = queue_product_recognition_for_partitions(partition_ids)

            job.status = HashRescanStatus.COMPLETED
            job.files_updated = updated
            job.files_total = total
            job.queued_analyses = queued
        except Exception as e:
            job.status = HashRescanStatus.FAILED
            job.error_message = str(e)
        finally:
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()


def _start_rescan(database_id):
    """Create a HashRescanJob and launch the background thread.

    Returns (job, None) on success or (None, error_message) if a rescan
    is already running.
    """
    running = HashRescanJob.query.filter_by(status=HashRescanStatus.RUNNING).first()
    if running:
        return None, 'A rescan is already in progress.'

    job = HashRescanJob(
        database_id=database_id,
        status=HashRescanStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(job)
    db.session.commit()

    app = current_app._get_current_object()
    t = threading.Thread(target=_run_rescan_background, args=(app, job.id), daemon=True)
    t.start()
    return job, None


@blueprint.route('/rescan', methods=['POST'])
@login_required
@require_permission('read_write')
def rescan_all():
    """Trigger a full collection-wide hash rescan (from the index page)."""
    job, err = _start_rescan(database_id=None)
    if err:
        flash(err, 'warning')
    else:
        flash('Hash rescan started in the background.', 'info')
    return redirect(url_for(f'{ROUTENAME}.index'))


@blueprint.route('/<int:id>/rescan', methods=['POST'])
@login_required
@require_permission('read_write')
def rescan(id):
    """Trigger a full collection-wide hash rescan (from a database view page)."""
    HashDatabase.query.get_or_404(id)
    job, err = _start_rescan(database_id=id)
    if err:
        flash(err, 'warning')
    else:
        flash('Hash rescan started in the background.', 'info')
    return redirect(url_for(f'{ROUTENAME}.view', id=id))


# =============================================================================
# Known Products
# =============================================================================

@blueprint.route('/<int:db_id>/products/new', methods=['POST'])
@login_required
@require_permission('read_write')
def new_known_product(db_id):
    HashDatabase.query.get_or_404(db_id)
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
    from ..utils.hash_rescan import (
        queue_product_recognition_for_partitions,
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
        partition_ids = set()
        for kf in changed_for_rescan:
            rescan_links_for_known_file_id(kf.id)
            rescan_hashes_for_known_file(kf)
            if enable_recognition:
                partition_ids |= _partition_ids_for_hashes(kf.md5, kf.sha1, kf.file_size)
        if enable_recognition and partition_ids:
            queue_product_recognition_for_partitions(partition_ids)

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

    kf_ids = [kf.id for kf in product.known_files]

    # Collect affected ExtractedFile IDs and partition IDs before unlinking.
    if kf_ids:
        affected_ef_ids = [
            row[0] for row in
            ExtractedFile.query
            .with_entities(ExtractedFile.id)
            .filter(ExtractedFile.known_file_id.in_(kf_ids))
            .all()
        ]
        if is_active and enable_recognition:
            pre_delete_partition_ids = {
                row[0] for row in
                ExtractedFile.query
                .with_entities(ExtractedFile.partition_id)
                .filter(ExtractedFile.id.in_(affected_ef_ids))
                .all()
            } if affected_ef_ids else set()
        else:
            pre_delete_partition_ids = set()
        # Clear FK references so the cascade delete cannot violate the constraint.
        if affected_ef_ids:
            ExtractedFile.query.filter(
                ExtractedFile.id.in_(affected_ef_ids)
            ).update({'known_file_id': None, 'is_known': False}, synchronize_session=False)
    else:
        affected_ef_ids = []
        pre_delete_partition_ids = set()

    db.session.delete(product)
    db.session.commit()
    flash(f'Product "{title}" deleted.', 'success')

    if is_active and affected_ef_ids:
        from ..utils.hash_rescan import queue_product_recognition_for_partitions, rescan_hashes_for_queryset
        # Re-evaluate the unlinked files; they may match another active database.
        rescan_hashes_for_queryset(ExtractedFile.query.filter(ExtractedFile.id.in_(affected_ef_ids)))
        if enable_recognition and pre_delete_partition_ids:
            queue_product_recognition_for_partitions(pre_delete_partition_ids)

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
    product.database.file_count = (product.database.file_count or 0) + 1
    db.session.commit()
    flash(f'File "{filename}" added to "{product.title}".', 'success')
    if product.database.is_active:
        from ..utils.hash_rescan import queue_product_recognition_for_partitions, rescan_hashes_for_known_file
        rescan_hashes_for_known_file(kf)
        if product.database.enable_product_recognition:
            partition_ids = _partition_ids_for_hashes(kf.md5, kf.sha1, kf.file_size)
            queue_product_recognition_for_partitions(partition_ids)
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
        from ..utils.hash_rescan import (
            queue_product_recognition_for_partitions,
            rescan_hashes_for_known_file,
            rescan_links_for_known_file_id,
        )
        # Re-evaluate files that were linked via the old hashes (they may
        # no longer match), then scan for files matching the new hashes.
        rescan_links_for_known_file_id(kf_id)
        rescan_hashes_for_known_file(kf)
        if enable_recognition:
            partition_ids = _partition_ids_for_hashes(kf.md5, kf.sha1, kf.file_size)
            queue_product_recognition_for_partitions(partition_ids)
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
    if is_active and enable_recognition:
        pre_delete_partition_ids = {
            row[0] for row in
            ExtractedFile.query
            .with_entities(ExtractedFile.partition_id)
            .filter(ExtractedFile.id.in_(affected_ef_ids))
            .all()
        } if affected_ef_ids else set()
    else:
        pre_delete_partition_ids = set()

    # Clear FK references before deletion to avoid constraint violation.
    if affected_ef_ids:
        ExtractedFile.query.filter(
            ExtractedFile.id.in_(affected_ef_ids)
        ).update({'known_file_id': None, 'is_known': False}, synchronize_session=False)

    db.session.delete(kf)
    if database.file_count and database.file_count > 0:
        database.file_count -= 1
    db.session.commit()
    flash(f'File "{filename}" deleted.', 'success')

    if is_active and affected_ef_ids:
        from ..utils.hash_rescan import queue_product_recognition_for_partitions, rescan_hashes_for_queryset
        # Re-evaluate unlinked files; they may match another active database.
        rescan_hashes_for_queryset(ExtractedFile.query.filter(ExtractedFile.id.in_(affected_ef_ids)))
        if enable_recognition and pre_delete_partition_ids:
            queue_product_recognition_for_partitions(pre_delete_partition_ids)

    return redirect(url_for(f'{ROUTENAME}.view_product', id=db_id, pid=pid))


# vim: ts=4 sw=4 et
