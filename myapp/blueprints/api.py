"""
Arcology - API Blueprint

RESTful API for external integrations.
"""

import hmac
import os
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, jsonify, request, current_app, send_file
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, update, or_
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError

from ..extensions import db, csrf
from ..database import (
    Item, Artefact, ArtefactType, Analysis, AnalysisType, AnalysisStatus,
    Partition, ExtractedFile, FilesystemType, Platform, Category, Tag,
    ExternalSystem, ExternalReference, HashDatabase, KnownFile, KnownProduct,
    RecognisedProduct, StorageDirectory,
    ApiKey, ApiKeyPermission, _API_KEY_PERMISSION_ORDER,
    ArtefactProtection, ArtefactMastering, RiscosModule,
    ExtractedFileRestriction,
)
from .artefacts import (
    get_artefact_path, _delete_artefact_files, _delete_item_files,
    detect_artefact_type, save_uploaded_file, compute_file_hashes,
    queue_analyses_for_artefact,
    _collect_all_file_restrictions, _collect_ancestor_file_restrictions,
)
from ..utils.hash_rescan import find_known_file
from ..utils.item_helpers import assign_item_fields, assign_item_tags
from ..utils.slugs import generate_slug, ensure_unique_slug
from ..utils.api_serializers import (
    item_to_dict, artefact_to_dict, analysis_to_dict,
    partition_to_dict, file_to_dict, known_file_to_dict,
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/api')


def init_app(app):
    """Exempt API from CSRF protection."""
    csrf.exempt(blueprint)


def error_response(message, status_code=400):
    return jsonify({'error': message}), status_code


# =============================================================================
# API Key Authentication
# =============================================================================

def _get_raw_key() -> str:
    """Extract API key from Authorization: Bearer or X-API-Key header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.headers.get('X-API-Key', '')


def _is_worker_request() -> bool:
    """Return True if the current request authenticated with the WORKER_API_KEY.

    Used to gate endpoints or fields that should only be accessible to the
    worker process, not to ordinary user API keys — even read_write ones.

    Rotation: if WORKER_API_KEY is compromised, stop all workers, update the
    value in .env (and on worker containers), then restart.  In-progress jobs
    will time out and be re-queued automatically.  Generate a new key with:
        python3 -c 'import secrets; print(f"wrk_{secrets.token_urlsafe(32)}")'
    """
    worker_key = current_app.config.get('WORKER_API_KEY', '')
    return bool(worker_key and hmac.compare_digest(_get_raw_key(), worker_key))


def require_auth(permission: str = 'read_only'):
    """
    Decorator that requires a valid API key with at least *permission* level.

    Checks the WORKER_API_KEY pre-shared secret first (always read_write),
    then falls back to user application keys stored in the database.

    The health check endpoint is intentionally left unauthenticated so that
    Docker / container orchestrators can reach it without credentials.
    """
    required_idx = _API_KEY_PERMISSION_ORDER.index(ApiKeyPermission(permission))

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            raw = _get_raw_key()

            # Pre-shared worker key — always grants read_write, no DB hit needed
            worker_key = current_app.config.get('WORKER_API_KEY', '')
            if worker_key and hmac.compare_digest(raw, worker_key):
                return f(*args, **kwargs)

            # User application key
            key = ApiKey.verify(raw)
            if not key:
                return error_response('Valid API key required', 401)
            eff_idx = _API_KEY_PERMISSION_ORDER.index(key.effective_permission())
            if eff_idx < required_idx:
                return error_response('Insufficient permissions', 403)
            # Throttle last_used_at updates to avoid a write transaction on
            # every single API call.  Only update if stale by >60 seconds.
            now = datetime.now(timezone.utc)
            if not key.last_used_at or (now - key.last_used_at).total_seconds() > 60:
                key.last_used_at = now
                db.session.commit()
            return f(*args, **kwargs)

        return wrapper
    return decorator


def _check_nul_bytes(data: dict, fields: list) -> str | None:
    """
    Check a flat dict for NUL characters (0x00) in string fields.

    PostgreSQL TEXT columns reject strings that contain NUL bytes, which
    causes an unhandled 500 rather than a clear error.  Calling this before
    any DB write lets the API return a descriptive 400 instead.

    Returns the first offending field name, or None if all fields are clean.
    """
    for field in fields:
        val = data.get(field)
        if isinstance(val, str) and '\x00' in val:
            return field
    return None


def _json_data(*, force: bool = False):
    """Return decoded JSON payload, preserving Flask's normal error handling."""
    return request.get_json(force=force)


def _json_object(*, force: bool = False, required: bool = False):
    """Return a JSON object payload or a ready-made error response."""
    data = _json_data(force=force)
    if data is None:
        if required:
            return None, error_response('JSON body required')
        return {}, None
    if not isinstance(data, dict):
        return None, error_response('JSON object required')
    return data, None


def _json_array(*, force: bool = False, required: bool = False):
    """Return a JSON array payload or a ready-made error response."""
    data = _json_data(force=force)
    if data is None:
        if required:
            return None, error_response('JSON body required')
        return [], None
    if not isinstance(data, list):
        return None, error_response('expected a JSON array')
    return data, None


def _require_fields(data: dict, *fields: str):
    """Return an error response if any required fields are missing."""
    missing = [field for field in fields if field not in data]
    if missing:
        return error_response(', '.join(missing) + ' are required')
    return None


def _nul_error(data: dict, fields: list[str]):
    """Return a standard NUL-byte validation error response, if needed."""
    bad_field = _check_nul_bytes(data, fields)
    if bad_field:
        return error_response(
            f"Field '{bad_field}' contains NUL characters (0x00) which are not permitted in text fields"
        )
    return None


def _validate_storage_path(path: str) -> bool:
    """Return True only if path is a safe relative filename with no traversal.

    Rejects absolute paths and any path whose normalised form contains a '..'
    component.  This is defence-in-depth: get_artefact_path() also enforces
    confinement at read/delete time.
    """
    if not path or os.path.isabs(path):
        return False
    return '..' not in os.path.normpath(path).split(os.sep)


from ..utils.db_helpers import get_by_uuid_or_404 as _get_by_uuid_or_404
from ..utils.db_helpers import get_by_id_or_404 as _get_by_id_or_404


def _get_item_or_404(uuid):
    return _get_by_uuid_or_404(Item, uuid)


def _get_artefact_or_404(uuid, *load_options):
    return _get_by_uuid_or_404(Artefact, uuid, *load_options)


def _get_analysis_or_404(*, id=None, uuid=None, load_options=()):
    if uuid is not None:
        return _get_by_uuid_or_404(Analysis, uuid, *load_options)
    return _get_by_id_or_404(Analysis, id, *load_options)


def _get_partition_or_404(uuid):
    return _get_by_uuid_or_404(Partition, uuid)


def _get_extracted_file_or_404(*, id=None, uuid=None, load_options=()):
    if uuid is not None:
        return _get_by_uuid_or_404(ExtractedFile, uuid, *load_options)
    return _get_by_id_or_404(ExtractedFile, id, *load_options)


def _get_hash_database_or_404(id):
    return _get_by_id_or_404(HashDatabase, id)


def _get_known_product_or_404(database_id, product_id):
    return KnownProduct.query.filter_by(id=product_id, database_id=database_id).first_or_404()


# =============================================================================
# Health Check
# =============================================================================

@blueprint.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for container orchestration."""
    try:
        # Verify database connectivity by executing a simple query
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        current_app.logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'error': 'Database unavailable'}), 503


# =============================================================================
# Items
# =============================================================================

@blueprint.route('/items', methods=['GET'])
@require_auth('read_only')
def list_items():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    query = Item.query

    if request.args.get('q'):
        search = f'%{request.args["q"]}%'
        query = query.filter(or_(Item.name.ilike(search), Item.description.ilike(search)))
    if request.args.get('platform_id', type=int):
        query = query.filter(Item.platform_id == request.args.get('platform_id', type=int))
    if request.args.get('category_id', type=int):
        query = query.filter(Item.category_id == request.args.get('category_id', type=int))
    if request.args.get('tag'):
        query = query.filter(Item.tags.any(Tag.name == request.args['tag']))

    # Eager-load relationships accessed by item_to_dict to avoid N+1
    query = query.options(
        selectinload(Item.platform),
        selectinload(Item.category),
        selectinload(Item.tags),
    )

    pagination = query.order_by(Item.name).paginate(page=page, per_page=per_page)

    # Batch artefact counts instead of loading all artefact objects per item
    item_ids = [item.id for item in pagination.items]
    artefact_counts = {}
    if item_ids:
        counts = (
            db.session.query(Artefact.item_id, func.count(Artefact.id))
            .filter(Artefact.item_id.in_(item_ids))
            .group_by(Artefact.item_id)
            .all()
        )
        artefact_counts = dict(counts)

    return jsonify({
        'items': [item_to_dict(item, _artefact_count=artefact_counts.get(item.id, 0))
                  for item in pagination.items],
        'total': pagination.total, 'page': page, 'per_page': per_page, 'pages': pagination.pages
    })


@blueprint.route('/items', methods=['POST'])
@require_auth('read_upload')
def create_item():
    data, error = _json_object(required=True)
    if error:
        return error
    if 'name' not in data:
        return error_response('Name is required')
    
    item = Item()
    assign_item_fields(
        item,
        name=data['name'],
        description=data.get('description'),
        platform_id=data.get('platform_id'),
        category_id=data.get('category_id'),
    )
    assign_item_tags(item, data.get('tags'))
    
    db.session.add(item)
    db.session.commit()
    item.slug = ensure_unique_slug(generate_slug(item.name), Item)
    db.session.commit()
    return jsonify(item_to_dict(item)), 201


@blueprint.route('/items/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_item(uuid):
    item = _get_item_or_404(uuid)
    return jsonify(item_to_dict(item, include_artefacts=True))


@blueprint.route('/items/<string:uuid>', methods=['PUT'])
@require_auth('read_write')
def update_item(uuid):
    item = _get_item_or_404(uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    if 'name' in data: item.name = data['name']
    if 'description' in data: item.description = data['description']
    if 'platform_id' in data: item.platform_id = data['platform_id']
    if 'category_id' in data: item.category_id = data['category_id']
    db.session.commit()
    return jsonify(item_to_dict(item))


@blueprint.route('/items/<string:uuid>', methods=['DELETE'])
@require_auth('read_write')
def delete_item(uuid):
    item = _get_item_or_404(uuid)
    _delete_item_files(item)
    db.session.delete(item)
    db.session.commit()
    return '', 204


# =============================================================================
# Artefacts
# =============================================================================

@blueprint.route('/items/<string:item_uuid>/artefacts', methods=['POST'])
@require_auth('read_upload')
def add_artefact(item_uuid):
    item = _get_item_or_404(item_uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    missing = _require_fields(data, 'label', 'storage_path', 'original_filename')
    if missing:
        return error_response('Label, storage_path and original_filename are required')

    try:
        artefact_type = ArtefactType(data.get('artefact_type', 'other'))
    except ValueError:
        return error_response('Invalid artefact_type')

    try:
        storage_directory = StorageDirectory(data.get('storage_directory', 'uploads'))
    except ValueError:
        return error_response('Invalid storage_directory')

    if not _validate_storage_path(data['storage_path']):
        return error_response('Invalid storage_path')

    artefact = Artefact(item_id=item.id, label=data['label'], artefact_type=artefact_type,
                        description=data.get('description'), original_filename=data['original_filename'],
                        storage_path=data['storage_path'], storage_directory=storage_directory,
                        file_size=data.get('file_size'), md5=data.get('md5'), sha256=data.get('sha256'))
    db.session.add(artefact)
    db.session.commit()
    artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': item.id})
    db.session.commit()
    return jsonify(artefact_to_dict(artefact)), 201


@blueprint.route('/artefacts/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_artefact(uuid):
    artefact = _get_artefact_or_404(uuid)
    return jsonify(artefact_to_dict(artefact, include_partitions=True))


@blueprint.route('/artefacts/<string:uuid>', methods=['DELETE'])
@require_auth('read_write')
def delete_artefact(uuid):
    artefact = _get_artefact_or_404(uuid)
    _delete_artefact_files(artefact)
    db.session.delete(artefact)
    db.session.commit()
    return '', 204


@blueprint.route('/artefacts/<string:uuid>', methods=['PATCH'])
@require_auth('read_upload')
def update_artefact(uuid):
    """Update mutable fields on an artefact (md5 and sha256)."""
    artefact = _get_artefact_or_404(uuid)
    data, error = _json_object()
    if error:
        return error
    if 'md5' in data:
        artefact.md5 = data['md5']
    if 'sha256' in data:
        artefact.sha256 = data['sha256']
    db.session.commit()
    return jsonify(artefact_to_dict(artefact))


@blueprint.route('/artefacts/<string:uuid>/download', methods=['GET'])
@require_auth('read_only')
def download_artefact(uuid):
    artefact = _get_artefact_or_404(uuid, selectinload(Artefact.restrictions))

    # Enforce download restrictions
    if artefact.restrictions:
        return jsonify({
            'error': 'Download restricted',
            'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        }), 403

    full_path = get_artefact_path(artefact)
    if not os.path.exists(full_path):
        return error_response('File not found', 404)
    return send_file(full_path, as_attachment=True, download_name=artefact.original_filename)


@blueprint.route('/files/<string:uuid>/download', methods=['GET'])
@require_auth('read_only')
def download_extracted_file(uuid):
    """Download an individual extracted file.  Restricted artefacts return 403."""
    ef = _get_extracted_file_or_404(uuid=uuid, load_options=(
        joinedload(ExtractedFile.partition)
        .joinedload(Partition.artefact)
        .selectinload(Artefact.restrictions)
    ,))
    artefact = ef.partition.artefact

    if artefact.restrictions:
        return jsonify({
            'error': 'Download restricted',
            'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        }), 403

    # Check file-level restrictions: own, descendants (archive contains restricted file),
    # and ancestors (file is inside a restricted archive).
    file_restrictions = (
        _collect_all_file_restrictions(ef) + _collect_ancestor_file_restrictions(ef)
    )
    if file_restrictions:
        return jsonify({
            'error': 'File download restricted',
            'restrictions': list({r.restriction_type.value for r in file_restrictions}),
        }), 403

    from .artefacts import _resolve_extracted_file_path
    file_path = _resolve_extracted_file_path(ef)
    if not file_path:
        return error_response('Extracted file not found on disk', 404)
    return send_file(file_path, as_attachment=True, download_name=ef.filename)


# =============================================================================
# Analysis
# =============================================================================

@blueprint.route('/artefacts/<string:uuid>/analysis', methods=['POST'])
@require_auth('read_upload')
def request_analysis(uuid):
    artefact = _get_artefact_or_404(uuid)
    data, error = _json_object()
    if error:
        return error
    try:
        analysis_type = AnalysisType(data.get('analysis_type', 'metadata_extract'))
    except ValueError:
        return error_response('Invalid analysis_type')

    nul_error = _nul_error(data, ['tool_name', 'hints'])
    if nul_error:
        return nul_error

    # Idempotency: return existing PENDING/RUNNING analysis instead of creating a
    # duplicate.  COMPLETED/FAILED analyses may be intentionally re-run, so only
    # active ones are considered.  This mirrors the logic in queue_analyses_for_artefact().
    # Include hints in the check so that multiple ARCHIVE_EXTRACT jobs for
    # different files within the same artefact are not collapsed into one.
    hints_json = data.get('hints')
    existing = Analysis.query.filter_by(
        artefact_id=artefact.id,
        analysis_type=analysis_type,
        hints=hints_json,
    ).filter(Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING])).first()
    if existing:
        return jsonify(analysis_to_dict(existing)), 200

    analysis = Analysis(
        artefact_id=artefact.id,
        analysis_type=analysis_type,
        status=AnalysisStatus.PENDING,
        tool_name=data.get('tool_name'),
        hints=hints_json
    )
    db.session.add(analysis)
    db.session.commit()
    return jsonify(analysis_to_dict(analysis)), 201


@blueprint.route('/artefacts/<string:uuid>/analysis', methods=['GET'])
@require_auth('read_only')
def get_artefact_analyses(uuid):
    artefact = _get_artefact_or_404(uuid)
    analyses = Analysis.query.filter_by(artefact_id=artefact.id).order_by(Analysis.id.desc()).all()
    return jsonify({'analyses': [analysis_to_dict(a) for a in analyses]})


@blueprint.route('/analysis/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_analysis(uuid):
    analysis = _get_analysis_or_404(uuid=uuid)
    return jsonify(analysis_to_dict(analysis))


@blueprint.route('/analysis/<int:id>', methods=['PUT'])
@require_auth('read_write')
def update_analysis(id):
    """
    Update analysis (used by worker).

    Supports atomic claiming: if claim_worker=True and status='running',
    uses an atomic database UPDATE to ensure only one worker can claim
    a job. This prevents race conditions when multiple workers try to
    claim the same job.
    """
    data, error = _json_object(required=True)
    if error:
        return error

    nul_error = _nul_error(data, [
        'tool_name', 'tool_version', 'output_url', 'output_path',
        'summary', 'details', 'error_message',
    ])
    if nul_error:
        return nul_error

    # Fields that drive the analysis lifecycle, downstream behaviour, or
    # operator-visible audit data may only be set by the worker process.
    # Ordinary read_write API keys cannot impersonate the worker by mutating
    # these values.
    _WORKER_ONLY_FIELDS = {
        'output_path', 'status', 'success', 'details',
        'tool_name', 'tool_version', 'summary', 'error_message', 'output_url',
    }
    worker = _is_worker_request()
    for _f in _WORKER_ONLY_FIELDS:
        if _f in data and not worker:
            return error_response(f'Only the worker may set {_f!r}', 403)

    # Handle atomic claim attempt using database-level atomicity
    if data.get('claim_worker') and data.get('status') == 'running':
        if not worker:
            return error_response('Only the worker may claim analysis jobs', 403)
        # Use atomic UPDATE with WHERE clause to prevent race conditions
        # Only one worker can successfully transition from PENDING to RUNNING
        result = db.session.execute(
            update(Analysis)
            .where(Analysis.id == id)
            .where(Analysis.status == AnalysisStatus.PENDING)
            .values(status=AnalysisStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        db.session.commit()

        # Fetch the analysis to return (also validates it exists)
        analysis = _get_analysis_or_404(id=id)
        response = analysis_to_dict(analysis)

        # Add 'claimed' field to indicate if THIS request actually claimed it
        response['claimed'] = result.rowcount > 0
        return jsonify(response)

    # Non-claim updates - use standard ORM approach
    analysis = _get_analysis_or_404(id=id)

    if 'status' in data:
        try:
            analysis.status = AnalysisStatus(data['status'])
        except ValueError:
            return error_response('Invalid status')

    for field in ['tool_name', 'tool_version', 'output_url', 'output_path',
                  'success', 'summary', 'details', 'error_message']:
        if field in data:
            setattr(analysis, field, data[field])

    if data.get('status') == 'running' and not analysis.started_at:
        analysis.started_at = datetime.now(timezone.utc)
    if data.get('status') in ('completed', 'failed'):
        analysis.completed_at = datetime.now(timezone.utc)

    # On successful completion of specific analysis types, extract structured
    # data from the JSON details blob into indexed search tables.
    if data.get('status') == 'completed' and data.get('success'):
        _populate_search_index(analysis)

    try:
        db.session.commit()
    except StaleDataError:
        # The analysis row was cascade-deleted (e.g. item/artefact deleted)
        # between our query and the commit.  This is not a server error —
        # return 404 so the worker's existing 404 handler discards the result.
        db.session.rollback()
        return jsonify({'error': 'Analysis was deleted during update'}), 404
    return jsonify(analysis_to_dict(analysis))


def _populate_search_index(analysis):
    """Extract structured search data from a completed analysis's JSON details blob.

    Called after update_analysis() marks a job as completed+successful.
    Handles DISC_PROTECTION_DETECT, DISC_MASTERING_DETECT, and PARTITION_DETECT.
    Errors are logged but never propagated — the analysis update itself must
    still succeed even if index population fails.
    """
    import json

    if not analysis.details:
        return

    try:
        details = json.loads(analysis.details)
    except (ValueError, TypeError):
        current_app.logger.warning(
            f"Could not parse details JSON for analysis {analysis.uuid} "
            f"({analysis.analysis_type.value}) — skipping search index update"
        )
        return

    try:
        if analysis.analysis_type == AnalysisType.DISC_PROTECTION_DETECT:
            # Delete any previous rows for this artefact so re-runs stay clean.
            ArtefactProtection.query.filter_by(artefact_id=analysis.artefact_id).delete()
            for ind in details.get('indicators', []):
                db.session.add(ArtefactProtection(
                    artefact_id=analysis.artefact_id,
                    protection_type=ind.get('type', 'unknown'),
                    track=ind.get('track'),
                    side=ind.get('side'),
                    details=ind.get('sector_id') or ind.get('details'),
                ))

        elif analysis.analysis_type == AnalysisType.DISC_MASTERING_DETECT:
            ArtefactMastering.query.filter_by(artefact_id=analysis.artefact_id).delete()
            for ind in details.get('indicators', []):
                _mtype = ind.get('type', 'unknown')
                if _mtype == 'bcd_timestamp':
                    _mtype = 'formaster'
                db.session.add(ArtefactMastering(
                    artefact_id=analysis.artefact_id,
                    mastering_type=_mtype,
                    track=ind.get('track'),
                    decoded=ind.get('decoded') or ind.get('data'),
                ))

        elif analysis.analysis_type == AnalysisType.PARTITION_DETECT:
            gnu_file_type = details.get('file', {}).get('file_type')
            if gnu_file_type:
                # Apply to all partitions belonging to this artefact.
                Partition.query.filter_by(artefact_id=analysis.artefact_id).update(
                    {'gnu_file_type': gnu_file_type}
                )

        elif analysis.analysis_type == AnalysisType.RISCOS_MODULE_PARSE:
            RiscosModule.query.filter_by(artefact_id=analysis.artefact_id).delete()
            for mod in details.get('modules', []):
                title = mod.get('title_string', '')
                if not title:
                    continue
                commands = mod.get('commands', [])
                commands_json = json.dumps([c['name'] for c in commands]) if commands else None
                raw_swis = mod.get('swi_names')
                if raw_swis and len(raw_swis) > 1:
                    swi_names_json = json.dumps([f"{raw_swis[0]}_{s}" for s in raw_swis[1:]])
                else:
                    swi_names_json = None
                db.session.add(RiscosModule(
                    artefact_id=analysis.artefact_id,
                    title_string=title,
                    help_title=mod.get('help_title'),
                    version=mod.get('version'),
                    date=mod.get('date'),
                    swi_chunk=mod.get('swi_chunk'),
                    file_path=mod.get('file_path'),
                    module_hash=mod.get('hash'),
                    commands=commands_json,
                    swi_names=swi_names_json,
                ))

    except Exception:
        current_app.logger.exception(
            f"Error populating search index for analysis {analysis.uuid} "
            f"({analysis.analysis_type.value})"
        )


@blueprint.route('/analysis/pending', methods=['GET'])
@require_auth('read_only')
def get_pending_analyses():
    """Get pending analyses (for worker)."""
    analyses = (
        Analysis.query
        .filter(Analysis.status == AnalysisStatus.PENDING)
        .options(joinedload(Analysis.artefact).joinedload(Artefact.item))
        .order_by(Analysis.created_at)
        .limit(50)
        .all()
    )
    return jsonify({'analyses': [analysis_to_dict(a, include_artefact=True) for a in analyses]})


@blueprint.route('/analysis/reset-stale', methods=['POST'])
@require_auth('read_write')
def reset_stale_analyses():
    """Reset RUNNING jobs stuck longer than the stale timeout back to PENDING.

    Called by the worker on startup to recover any jobs left in RUNNING state
    by a previous worker crash.  Also callable by operators via the UI.
    """
    timeout_seconds = current_app.config.get('STALE_JOB_TIMEOUT_SECONDS', 3600)
    # started_at is stored as naive UTC
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=timeout_seconds)
    stale = Analysis.query.filter(
        Analysis.status == AnalysisStatus.RUNNING,
        Analysis.started_at < cutoff,
    ).all()
    for analysis in stale:
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
    if stale:
        current_app.logger.info(f'Reset {len(stale)} stale analysis job(s) to PENDING')
    return jsonify({'reset': len(stale)})


# =============================================================================
# Output Files
# =============================================================================

@blueprint.route('/outputs/<path:filename>', methods=['GET'])
@require_auth('read_only')
def get_output_file(filename):
    """Serve an output file (visualisation, etc.)."""
    output_dir = current_app.config.get('OUTPUT_FOLDER', 'outputs')
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(current_app.instance_path, output_dir)
    
    # Security: resolve the full path and ensure it stays within output_dir
    file_path = os.path.realpath(os.path.join(output_dir, filename))
    if not file_path.startswith(os.path.realpath(output_dir) + os.sep):
        return error_response('File not found', 404)
    
    if not os.path.exists(file_path):
        return error_response('File not found', 404)
    
    return send_file(file_path)


# =============================================================================
# Partitions & Files
# =============================================================================

@blueprint.route('/analysis/<int:id>/produce-artefact', methods=['POST'])
@require_auth('read_upload')
def produce_artefact(id):
    """
    Create a derived artefact from an analysis result.
    Used by workers when e.g. flux decode produces a sector image.
    The new artefact will automatically have follow-on analyses queued.
    """
    if not _is_worker_request():
        return error_response('Only the worker may register derived artefacts', 403)
    analysis = _get_analysis_or_404(id=id)
    data, error = _json_object(required=True)
    if error:
        return error

    missing = _require_fields(data, 'label', 'original_filename', 'storage_path', 'artefact_type')
    if missing:
        return missing

    nul_error = _nul_error(data, ['label', 'original_filename', 'description', 'storage_path'])
    if nul_error:
        return nul_error

    if not _validate_storage_path(data['storage_path']):
        return error_response('Invalid storage_path')

    try:
        artefact_type = ArtefactType(data['artefact_type'])
    except ValueError:
        return error_response('Invalid artefact_type')

    # Determine storage directory - derived artefacts default to outputs
    storage_dir_value = data.get('storage_directory', 'outputs')
    try:
        storage_directory = StorageDirectory(storage_dir_value)
    except ValueError:
        storage_directory = StorageDirectory.OUTPUTS

    # Idempotency: if a worker retries (e.g. after a network timeout) and this
    # exact artefact was already registered for this analysis run, return the
    # existing record rather than inserting a duplicate.  The combination of
    # (derived_from_analysis_id, storage_path) uniquely identifies a derived
    # artefact within a single analysis run.
    existing_artefact = Artefact.query.filter_by(
        derived_from_analysis_id=analysis.id,
        storage_path=data['storage_path'],
    ).first()
    if existing_artefact:
        current_app.logger.info(
            f"produce_artefact: returning existing artefact {existing_artefact.uuid} "
            f"(idempotent retry for analysis {analysis.id})"
        )
        return jsonify({
            'artefact': artefact_to_dict(existing_artefact),
            'queued_analyses': [],
        }), 200

    # On the first produce_artefact call for this analysis, remove any derived
    # artefacts that were created by a previous analysis of the same type on the
    # same source artefact (e.g. re-running PARTITION_DETECT).  We detect "first
    # call" by checking whether any artefacts are already linked to this analysis;
    # subsequent calls within the same run are harmless because the old ones are
    # already gone by then.
    already_produced = Artefact.query.filter_by(
        derived_from_analysis_id=analysis.id
    ).count()

    if already_produced == 0:
        # Only clean up artefacts from completed or failed analyses.
        # Excluding PENDING/RUNNING prevents concurrent sibling workers (e.g.
        # multiple archive_extract jobs for different nested archives of the same
        # parent) from treating each other's newly-created artefacts as "prior"
        # and racing to delete them, causing FK violations.
        prior_derived = (
            Artefact.query
            .join(Analysis, Artefact.derived_from_analysis_id == Analysis.id)
            .filter(
                Artefact.parent_artefact_id == analysis.artefact_id,
                Analysis.analysis_type == analysis.analysis_type,
                Artefact.derived_from_analysis_id != analysis.id,
                Analysis.status.in_([AnalysisStatus.COMPLETED, AnalysisStatus.FAILED]),
            )
            .all()
        )
        # Use no_autoflush to prevent SQLAlchemy from flushing pending deletes
        # mid-loop when lazy-loading derived_artefacts inside _delete_artefact_files.
        with db.session.no_autoflush:
            for prior in prior_derived:
                current_app.logger.info(
                    f"Removing prior derived artefact {prior.uuid} "
                    f"(from previous {analysis.analysis_type.value} analysis) "
                    f"before re-run"
                )
                _delete_artefact_files(prior)
                db.session.delete(prior)
        if prior_derived:
            db.session.flush()

    # Create derived artefact
    artefact = Artefact(
        item_id=analysis.artefact.item_id,
        label=data['label'],
        artefact_type=artefact_type,
        type_overridden=False,
        description=data.get('description'),
        original_filename=data['original_filename'],
        storage_path=data['storage_path'],
        storage_directory=storage_directory,
        file_size=data.get('file_size'),
        mime_type=data.get('mime_type'),
        md5=data.get('md5'),
        sha256=data.get('sha256'),
        parent_artefact_id=analysis.artefact_id,
        derived_from_analysis_id=analysis.id
    )
    
    db.session.add(artefact)
    db.session.commit()

    # Generate slug (unique within this item)
    artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': analysis.artefact.item_id})
    db.session.commit()

    # Queue follow-on analyses unless the caller will handle that explicitly
    queued_analyses = []
    if data.get('auto_analyse', True):
        from .artefacts import queue_analyses_for_artefact, ANALYSIS_MAP

        # Pass through any hints from parent analysis
        hints = None
        if analysis.hints:
            import json
            hints = json.loads(analysis.hints)

        queue_analyses_for_artefact(artefact, hints)
        queued_analyses = [t.value for t in ANALYSIS_MAP.get(artefact_type, [])]

    return jsonify({
        'artefact': artefact_to_dict(artefact),
        'queued_analyses': queued_analyses
    }), 201


@blueprint.route('/artefacts/<string:uuid>/partitions', methods=['POST'])
@require_auth('read_upload')
def add_partition(uuid):
    if not _is_worker_request():
        return error_response('Only the worker may register partitions', 403)
    artefact = _get_artefact_or_404(uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    try:
        filesystem = FilesystemType(data.get('filesystem', 'unknown'))
    except ValueError:
        return error_response('Invalid filesystem')

    partition_index = data.get('partition_index', 0)

    # Delete ALL existing partitions with this index (handles duplicates from previous runs)
    existing_partitions = Partition.query.filter_by(
        artefact_id=artefact.id,
        partition_index=partition_index
    ).all()

    if existing_partitions:
        # Delete all existing partitions (cascade will delete all files)
        for existing in existing_partitions:
            current_app.logger.info(f"Deleting existing partition {existing.uuid} (index {partition_index}) for artefact {uuid}")
            db.session.delete(existing)
        db.session.flush()  # Ensure deletes are processed before creating new one

    partition = Partition(artefact_id=artefact.id, partition_index=partition_index,
                          label=data.get('label'), filesystem=filesystem,
                          container_format=data.get('container_format'),
                          total_files=data.get('total_files'), total_bytes=data.get('total_bytes'))
    db.session.add(partition)
    db.session.commit()
    return jsonify(partition_to_dict(partition)), 201


@blueprint.route('/partitions/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_partition(uuid):
    """Get partition details by UUID."""
    partition = _get_partition_or_404(uuid)
    return jsonify(partition_to_dict(partition))


@blueprint.route('/partitions/<string:uuid>/files', methods=['POST'])
@require_auth('read_upload')
def add_files(uuid):
    if not _is_worker_request():
        return error_response('Only the worker may register extracted files', 403)
    partition = _get_partition_or_404(uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    if 'files' not in data:
        return error_response('files array required')

    added = 0
    skipped = 0
    for f in data['files']:
        if 'path' not in f or 'filename' not in f:
            continue

        # If this file was extracted from an archive (has parent_file_id),
        # nest it under the parent archive's path so the archive appears
        # as a virtual directory in the UI.
        path = f['path']
        parent_file_id = f.get('parent_file_id')
        if parent_file_id:
            parent_file = ExtractedFile.query.get(parent_file_id)
            if parent_file and parent_file.is_archive:
                if not path.startswith(parent_file.path + '/'):
                    path = parent_file.path + '/' + path

        # Check if this file already exists in the partition (prevents duplicates)
        existing_file = ExtractedFile.query.filter_by(
            partition_id=partition.id,
            path=path
        ).first()

        if existing_file:
            skipped += 1
            continue

        ef = ExtractedFile(
            partition_id=partition.id,
            path=path,
            filename=f['filename'],
            extension=f.get('extension'),
            file_size=f.get('file_size'),
            md5=f.get('md5'),
            sha1=f.get('sha1'),
            sha256=f.get('sha256'),
            crc32=f.get('crc32'),
            # Archive support fields
            is_directory=f.get('is_directory', False),
            risc_os_filetype=f.get('risc_os_filetype'),
            parent_file_id=f.get('parent_file_id'),
            extraction_depth=f.get('extraction_depth', 0)
        )

        if ef.md5 or ef.sha1:
            known = find_known_file(md5=ef.md5, sha1=ef.sha1, file_size=ef.file_size)
            if known:
                ef.known_file_id = known.id
                ef.is_known = True
        db.session.add(ef)
        added += 1

    partition.total_files = ExtractedFile.query.filter_by(partition_id=partition.id).count()
    partition.unique_files = ExtractedFile.query.filter_by(partition_id=partition.id, is_known=False).count()
    db.session.commit()

    # Auto-apply restrictions from flagged hash databases
    if added > 0:
        from ..utils.hash_rescan import apply_database_restrictions
        apply_database_restrictions(partition.artefact)

    response = {'added': added}
    if skipped > 0:
        response['skipped'] = skipped
        current_app.logger.info(f"Skipped {skipped} duplicate files for partition {uuid}")
    return jsonify(response)


@blueprint.route('/partitions/<string:uuid>/files', methods=['GET'])
@require_auth('read_only')
def get_partition_files(uuid):
    partition = _get_partition_or_404(uuid)
    show_known = request.args.get('show_known', 'false').lower() == 'true'
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)

    query = ExtractedFile.query.filter_by(partition_id=partition.id)
    if not show_known:
        query = query.filter(ExtractedFile.is_known == False)

    # Filter by is_archive if specified (used by ARCHIVE_DETECT to skip already-detected files)
    is_archive_param = request.args.get('is_archive')
    if is_archive_param is not None:
        query = query.filter(ExtractedFile.is_archive == (is_archive_param.lower() == 'true'))

    pagination = query.order_by(ExtractedFile.path).paginate(page=page, per_page=per_page)
    return jsonify({
        'files': [file_to_dict(f) for f in pagination.items],
        'total': pagination.total, 'page': page, 'per_page': per_page, 'pages': pagination.pages
    })


@blueprint.route('/files/<int:file_id>/mark_archive', methods=['POST'])
@require_auth('read_upload')
def mark_file_as_archive(file_id):
    """Mark a file as an archive and update its metadata."""
    file = _get_extracted_file_or_404(id=file_id)
    data, error = _json_object(required=True)
    if error:
        return error

    file.is_archive = data.get('is_archive', True)
    file.archive_format = data.get('archive_format')

    db.session.commit()
    return jsonify(file_to_dict(file))


# =============================================================================
# Lookup
# =============================================================================

@blueprint.route('/lookup', methods=['GET'])
@require_auth('read_only')
def lookup_by_external():
    system_name = request.args.get('system')
    external_id = request.args.get('external_id')
    if not system_name or not external_id:
        return error_response('system and external_id required')
    
    system = ExternalSystem.query.filter_by(name=system_name).first()
    if not system:
        return error_response('External system not found', 404)
    
    ref = ExternalReference.query.filter_by(system_id=system.id, external_id=external_id).first()
    if not ref:
        return error_response('Reference not found', 404)
    
    return jsonify(item_to_dict(ref.item, include_artefacts=True))


@blueprint.route('/hash-lookup', methods=['GET'])
@require_auth('read_only')
def hash_lookup():
    md5 = request.args.get('md5')
    sha1 = request.args.get('sha1')
    if not md5 and not sha1:
        return error_response('md5 or sha1 required')
    
    known = find_known_file(md5=md5, sha1=sha1)
    query = ExtractedFile.query
    if md5:
        query = query.filter(ExtractedFile.md5 == md5.lower())
    elif sha1:
        query = query.filter(ExtractedFile.sha1 == sha1.lower())
    
    extracted = query.options(
        joinedload(ExtractedFile.partition).joinedload(Partition.artefact).joinedload(Artefact.item)
    ).all()
    return jsonify({
        'known_file': known_file_to_dict(known) if known else None,
        'found_in': [{'artefact_id': f.partition.artefact_id, 'item_id': f.partition.artefact.item_id,
                      'item_name': f.partition.artefact.item.name, 'path': f.path} for f in extracted]
    })


# =============================================================================
# Taxonomy
# =============================================================================

@blueprint.route('/platforms', methods=['GET'])
@require_auth('read_only')
def list_platforms():
	"""List all platforms."""
	platforms = Platform.query.order_by(Platform.name).all()
	return jsonify({'platforms': [
		{'id': p.id, 'name': p.name, 'description': p.description,
		 'parent_id': p.parent_id}
		for p in platforms
	]})


@blueprint.route('/categories', methods=['GET'])
@require_auth('read_only')
def list_categories():
	"""List all categories."""
	categories = Category.query.order_by(Category.name).all()
	return jsonify({'categories': [
		{'id': c.id, 'name': c.name, 'description': c.description,
		 'parent_id': c.parent_id}
		for c in categories
	]})


@blueprint.route('/tags', methods=['GET'])
@require_auth('read_only')
def list_tags():
	"""List all tags."""
	tags = Tag.query.order_by(Tag.name).all()
	return jsonify({'tags': [
		{'id': t.id, 'name': t.name}
		for t in tags
	]})


# =============================================================================
# File Upload
# =============================================================================

@blueprint.route('/items/<string:item_uuid>/artefacts/upload', methods=['POST'])
@require_auth('read_upload')
def upload_artefact(item_uuid):
	"""
	Upload a file as a new artefact on an item.

	Accepts multipart form data with:
	  - file: the file to upload (required)
	  - label: display label for the artefact (required)
	  - artefact_type: override auto-detection (optional, default 'auto')
	  - description: artefact description (optional)
	  - auto_analyse: queue automatic analyses (optional, default 'true')

	Returns the created artefact JSON with 201 status.
	"""
	item = _get_item_or_404(item_uuid)

	if 'file' not in request.files:
		return error_response('No file provided')
	file = request.files['file']
	if file.filename == '':
		return error_response('No file selected')

	label = request.form.get('label')
	if not label:
		return error_response('Label is required')

	# Save file with UUID-based name
	storage_name, file_size = save_uploaded_file(file)

	# Determine artefact type: use override if provided, otherwise auto-detect
	type_override = request.form.get('artefact_type', 'auto')
	type_overridden = False
	if type_override and type_override != 'auto':
		try:
			artefact_type = ArtefactType(type_override)
			type_overridden = True
		except ValueError:
			return error_response(f'Invalid artefact_type: {type_override}')
	else:
		artefact_type = detect_artefact_type(file.filename)

	# Compute hashes
	from .artefacts import get_upload_folder
	full_path = os.path.join(get_upload_folder(), storage_name)
	md5, sha256 = compute_file_hashes(full_path)

	# Preserve original filename
	from werkzeug.utils import secure_filename
	original_filename = secure_filename(file.filename) or 'unnamed'

	description = request.form.get('description')

	artefact = Artefact(
		item_id=item.id,
		label=label,
		artefact_type=artefact_type,
		type_overridden=type_overridden,
		description=description,
		original_filename=original_filename,
		storage_path=storage_name,
		storage_directory=StorageDirectory.UPLOADS,
		file_size=file_size,
		md5=md5,
		sha256=sha256,
	)
	db.session.add(artefact)
	db.session.commit()

	# Generate slug (unique within this item)
	artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': item.id})
	db.session.commit()

	# Optionally queue analyses
	queued_analyses = []
	auto_analyse = request.form.get('auto_analyse', 'true').lower() != 'false'
	if auto_analyse:
		from .artefacts import ANALYSIS_MAP
		queue_analyses_for_artefact(artefact)
		queued_analyses = [t.value for t in ANALYSIS_MAP.get(artefact_type, [])]

	result = artefact_to_dict(artefact)
	result['queued_analyses'] = queued_analyses
	return jsonify(result), 201


# =============================================================================
# Hash Database API (for CLI import/export and worker recognition)
# =============================================================================

@blueprint.route('/hash-databases', methods=['GET'])
@require_auth('read_only')
def list_hash_databases():
    databases = HashDatabase.query.order_by(HashDatabase.name).all()
    return jsonify([{
        'id': db_.id,
        'name': db_.name,
        'description': db_.description,
        'version': db_.version,
        'file_count': db_.file_count or 0,
        'enable_product_recognition': db_.enable_product_recognition,
    } for db_ in databases])


@blueprint.route('/hash-databases/<int:id>', methods=['GET'])
@require_auth('read_only')
def get_hash_database(id):
    database = _get_hash_database_or_404(id)
    products = KnownProduct.query.filter_by(database_id=id).order_by(KnownProduct.title).all()
    return jsonify({
        'id': database.id,
        'name': database.name,
        'description': database.description,
        'version': database.version,
        'source_url': database.source_url,
        'enable_product_recognition': database.enable_product_recognition,
        'products': [
            {
                'id': p.id,
                'title': p.title,
                'description': p.description,
                'path_match_enabled': p.path_match_enabled,
                'files': [
                    {
                        'id': kf.id,
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
    })


@blueprint.route('/hash-databases', methods=['POST'])
@require_auth('read_write')
def create_hash_database():
    data, error = _json_object(force=True)
    if error:
        return error
    if not data.get('name'):
        return error_response('name is required')
    if HashDatabase.query.filter_by(name=data['name']).first():
        return error_response(f"Database '{data['name']}' already exists", 409)
    database = HashDatabase(
        name=data['name'],
        description=data.get('description'),
        version=data.get('version'),
        source_url=data.get('source_url'),
        enable_product_recognition=bool(data.get('enable_product_recognition', False)),
        file_count=0,
    )
    db.session.add(database)
    db.session.commit()
    return jsonify({'id': database.id, 'name': database.name}), 201


@blueprint.route('/hash-databases/<int:db_id>/products', methods=['POST'])
@require_auth('read_write')
def create_known_product(db_id):
    database = _get_hash_database_or_404(db_id)
    data, error = _json_object(force=True)
    if error:
        return error
    if not data.get('title'):
        return error_response('title is required')
    product = KnownProduct(
        database_id=db_id,
        title=data['title'],
        description=data.get('description'),
        path_match_enabled=bool(data.get('path_match_enabled', False)),
    )
    db.session.add(product)
    db.session.commit()
    return jsonify({'id': product.id, 'title': product.title}), 201


@blueprint.route('/hash-databases/<int:db_id>/products/<int:pid>/files', methods=['POST'])
@require_auth('read_write')
def add_known_files_bulk(db_id, pid):
    database = _get_hash_database_or_404(db_id)
    product = _get_known_product_or_404(db_id, pid)
    data = _json_data(force=True)
    if data is None:
        data = {}
    if not isinstance(data, (dict, list)):
        return error_response('JSON object or array required')
    files = data if isinstance(data, list) else data.get('files', [])
    if not files:
        return error_response('files array is required')
    added = 0
    for f in files:
        if not f.get('filename'):
            continue
        kf = KnownFile(
            database_id=db_id,
            product_id=pid,
            filename=f['filename'],
            file_size=f.get('file_size'),
            md5=f.get('md5', '').lower() or None,
            sha1=f.get('sha1', '').lower() or None,
            sha256=f.get('sha256', '').lower() or None,
            crc32=f.get('crc32', '').lower() or None,
            is_required=bool(f.get('is_required', True)),
            relative_path=f.get('relative_path'),
            description=f.get('description'),
        )
        db.session.add(kf)
        added += 1
    database.file_count = (database.file_count or 0) + added
    db.session.commit()
    return jsonify({'added': added}), 201


@blueprint.route('/hash-databases/recognition-config', methods=['GET'])
@require_auth('read_only')
def hash_database_recognition_config():
    """Return all hash databases with enable_product_recognition=True, with full product/file data for the worker."""
    databases = HashDatabase.query.filter_by(enable_product_recognition=True).all()
    result = []
    for database in databases:
        products = KnownProduct.query.filter_by(database_id=database.id).all()
        db_entry = {
            'database_id': database.id,
            'name': database.name,
            'products': [],
        }
        for product in products:
            required = [kf for kf in product.known_files if kf.is_required]
            optional = [kf for kf in product.known_files if not kf.is_required]
            db_entry['products'].append({
                'product_id': product.id,
                'title': product.title,
                'path_match_enabled': product.path_match_enabled,
                'required_files': [
                    {'md5': kf.md5, 'sha1': kf.sha1, 'sha256': kf.sha256, 'relative_path': kf.relative_path}
                    for kf in required
                ],
                'optional_files': [
                    {'md5': kf.md5, 'sha1': kf.sha1, 'sha256': kf.sha256, 'relative_path': kf.relative_path}
                    for kf in optional
                ],
            })
        result.append(db_entry)
    return jsonify(result)


@blueprint.route('/partitions/<uuid>/recognised-products', methods=['POST'])
@require_auth('read_write')
def report_recognised_products(uuid):
    """Worker reports product recognition results for a partition."""
    partition = _get_partition_or_404(uuid)
    data, error = _json_array(force=True)
    if error:
        return error

    # Delete existing recognition results for this partition (re-scan replaces old results)
    RecognisedProduct.query.filter_by(partition_id=partition.id).delete()

    for entry in data:
        product_id = entry.get('product_id')
        folder_path = entry.get('folder_path', '')
        if not product_id or not folder_path:
            continue
        product = KnownProduct.query.get(product_id)
        if not product:
            continue
        rp = RecognisedProduct(
            partition_id=partition.id,
            product_id=product_id,
            folder_path=folder_path,
            required_matched=int(entry.get('required_matched', 0)),
            required_total=int(entry.get('required_total', 0)),
            optional_matched=int(entry.get('optional_matched', 0)),
            optional_total=int(entry.get('optional_total', 0)),
        )
        db.session.add(rp)

    db.session.commit()
    return jsonify({'status': 'ok'})


# vim: ts=4 sw=4 et
