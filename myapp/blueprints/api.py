"""
Arcology - API Blueprint

RESTful API for external integrations.
"""

import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Blueprint, abort, current_app, g, jsonify, request
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import contains_eager, joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError
from arcology_shared.enums import COMPRESSED_RAW_SECTOR_TYPES
from ..database import (
    _API_KEY_PERMISSION_ORDER,
    Analysis,
    AnalysisStatus,
    AnalysisType,
    ApiKey,
    ApiKeyPermission,
    Artefact,
    ArtefactType,
    Category,
    ExternalReference,
    ExternalSystem,
    ExtractedFile,
    FilesystemType,
    Group,
    HashDatabase,
    Item,
    ItemShare,
    KnownFile,
    KnownProduct,
    Partition,
    Platform,
    RecognisedProduct,
    StorageDirectory,
    Tag,
    User,
    UserPermission,
)
from ..extensions import csrf, db
from ..services import chunked_upload as _chunked
from ..services.artefact_lifecycle import (
    ArtefactMoveError,
    bulk_delete_item,
    collect_all_analyses,
    delete_artefact_files,
    move_artefact_to_item,
    validate_artefact_move,
)
from ..services.artefact_storage import (
    compute_file_hashes,
    get_artefact_storage_key,
    save_uploaded_file,
)
from ..services.artefact_types import detect_artefact_type, queue_analyses_for_artefact
from ..services.downloads import (
    resolve_output_artefact,
    serve_artefact_file,
    serve_extracted_file,
    serve_output_file,
)
from ..services.hash_rescan import (
    find_known_file,
    find_known_files_for_records,
    link_new_known_files,
)
from ..services.restrictions import (
    artefact_contained_file_restrictions,
    collect_all_file_restrictions,
    collect_ancestor_file_restrictions,
)
from ..services.upload_pipeline import QUEUE_FULL, QUEUE_NONE, ingest_uploaded_artefact
from ..utils.api_serializers import (
    analysis_to_dict,
    artefact_to_dict,
    file_to_dict,
    item_to_dict,
    known_file_to_dict,
    partition_to_dict,
    share_to_dict,
)
from ..utils.blobs import artefact_blob, artefact_blob_storage_path, assign_blob
from ..utils.db_helpers import get_by_id_or_404 as _get_by_id_or_404
from ..utils.db_helpers import get_by_uuid_or_404 as _get_by_uuid_or_404
from ..utils.enum_display import enum_value
from ..utils.item_helpers import assign_item_fields, assign_item_tags
from ..utils.privacy import recompute_item_privacy
from ..utils.slugs import ensure_unique_slug, generate_slug
from ..utils.slugs import lookup_by_identifier as _lookup_by_identifier
from ..visibility import (
    SHARE_PERMISSIONS,
    artefact_visibility_clause,
    can_change_owner,
    can_claim_item,
    can_contribute_to_item,
    can_curate_item,
    can_download_despite_restrictions,
    can_manage_privacy,
    can_manage_shares,
    can_view_artefact,
    can_view_item,
    item_visibility_clause,
    output_blocked_for,
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/api')


def init_app(app):
    """Exempt API from CSRF protection."""
    csrf.exempt(blueprint)


def error_response(message, status_code=400, **extra):
    """Return a JSON error response.

    ``extra`` keyword arguments are merged into the response body alongside
    ``'error'`` — used by restriction-gate endpoints to include the
    ``'restrictions'`` list so callers can distinguish restriction types.
    """
    payload = {'error': message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status_code


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
                g.api_is_worker = True
                g.api_user = None
                return f(*args, **kwargs)

            # User application key
            key = ApiKey.verify(raw)
            if not key:
                return error_response('Valid API key required', 401)
            eff_idx = _API_KEY_PERMISSION_ORDER.index(key.effective_permission())
            if eff_idx < required_idx:
                return error_response('Insufficient permissions', 403)
            g.api_is_worker = False
            g.api_user = key.user
            # Throttle last_used_at updates to avoid a write transaction on
            # every single API call.  Only update if stale by >60 seconds.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
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


def _api_viewer():
    """Return (user_or_None, sees_all) for the current API caller.

    The worker authenticates with the pre-shared key and must be able to read
    private content it has been asked to process, so it sees everything.  User
    API keys are scoped to their owning user's visibility.
    """
    return getattr(g, 'api_user', None), bool(getattr(g, 'api_is_worker', False))


def _require_view_item(item):
    """Abort with 404 if the API caller may not view *item*."""
    user, sees_all = _api_viewer()
    if not can_view_item(item, user, sees_all=sees_all):
        abort(404)


def _require_view_artefact(artefact):
    """Abort with 404 if the API caller may not view *artefact*."""
    user, sees_all = _api_viewer()
    if not can_view_artefact(artefact, user, sees_all=sees_all):
        abort(404)


def _require_view_analysis(analysis):
    """Abort with 404 if the API caller may not view an analysis' artefact.

    Analyses with no artefact (CLEANUP jobs queued by bulk item deletion)
    are system-internal: their hints contain storage keys, so only the
    worker may see them.
    """
    if analysis.artefact is None:
        _, sees_all = _api_viewer()
        if not sees_all:
            abort(404)
        return
    _require_view_artefact(analysis.artefact)


def _require_view_partition(partition):
    """Abort with 404 if the API caller may not view a partition's artefact."""
    _require_view_artefact(partition.artefact)


def _require_view_extracted_file(file):
    """Abort with 404 if the API caller may not view a file's artefact."""
    _require_view_partition(file.partition)


def _require_manage_item_content(item):
    """Return a 403 response if caller may not add or modify content on item."""
    api_user, sees_all = _api_viewer()
    if item.private_effective and not can_contribute_to_item(item, api_user, sees_all=sees_all):
        return error_response('Not permitted to modify this item', 403)
    return None


def _get_item_or_404(uuid):
    item = _lookup_by_identifier(Item, uuid)
    _require_view_item(item)
    return item


def _get_artefact_or_404(uuid, *load_options):
    artefact = _get_by_uuid_or_404(Artefact, uuid, *load_options)
    _require_view_artefact(artefact)
    return artefact


def _get_analysis_or_404(*, id=None, uuid=None, load_options=()):
    if uuid is not None:
        analysis = _get_by_uuid_or_404(Analysis, uuid, *load_options)
    else:
        analysis = _get_by_id_or_404(Analysis, id, *load_options)
    _require_view_analysis(analysis)
    return analysis


def _get_partition_or_404(uuid):
    partition = _get_by_uuid_or_404(Partition, uuid)
    _require_view_partition(partition)
    return partition


def _get_extracted_file_or_404(*, id=None, uuid=None, load_options=()):
    if uuid is not None:
        file = _get_by_uuid_or_404(ExtractedFile, uuid, *load_options)
    else:
        file = _get_by_id_or_404(ExtractedFile, id, *load_options)
    _require_view_extracted_file(file)
    return file


def _has_restricting_hash_database():
    """True if any active hash database carries a restriction_type.

    Cheap guard for the file-registration hot path: when no database is
    flagged, apply_database_restrictions() can only ever be a no-op, so the
    per-batch O(files) re-scan it performs is pure overhead and is skipped.
    """
    return db.session.query(
        HashDatabase.query.filter(
            HashDatabase.is_active.is_(True),
            HashDatabase.restriction_type.isnot(None),
        ).exists()
    ).scalar()


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
    _user, _sees_all = _api_viewer()
    query = Item.query.filter(item_visibility_clause(_user, sees_all=_sees_all))

    if request.args.get('q'):
        search = f'%{request.args["q"]}%'
        query = query.filter(or_(Item.name.ilike(search), Item.description.ilike(search)))
    if request.args.get('platform_id', type=int):
        query = query.filter(Item.platform_id == request.args.get('platform_id', type=int))
    if request.args.get('category_id', type=int):
        query = query.filter(Item.category_id == request.args.get('category_id', type=int))
    if request.args.get('tag'):
        query = query.filter(Item.tags.any(Tag.name == request.args['tag']))
    # Filter by parent: ?parent_uuid=<uuid> or ?parent_uuid=none for root items only
    parent_uuid_param = request.args.get('parent_uuid')
    if parent_uuid_param is not None:
        if parent_uuid_param.lower() in ('none', 'null', ''):
            query = query.filter(Item.parent_id.is_(None))
        else:
            parent = Item.query.filter(Item.uuid == parent_uuid_param).first()
            if parent is None:
                return error_response('Parent item not found', 404)
            query = query.filter(Item.parent_id == parent.id)

    # Eager-load relationships accessed by item_to_dict to avoid N+1
    query = query.options(
        selectinload(Item.platform),
        selectinload(Item.category),
        selectinload(Item.tags),
        selectinload(Item.parent),
        selectinload(Item.children),
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

    api_user, sees_all = _api_viewer()
    parent_id = None
    if data.get('parent_uuid'):
        parent = Item.query.filter(Item.uuid == data['parent_uuid']).first()
        if parent is None:
            return error_response('Parent item not found', 404)
        if not can_view_item(parent, api_user, sees_all=sees_all):
            return error_response('Parent item not found', 404)
        if parent.private_effective and not can_contribute_to_item(parent, api_user, sees_all=sees_all):
            return error_response('Not permitted to create child items under this parent', 403)
        parent_id = parent.id

    item = Item()
    assign_item_fields(
        item,
        name=data['name'],
        description=data.get('description'),
        platform_id=data.get('platform_id'),
        category_id=data.get('category_id'),
        parent_id=parent_id,
    )
    item.owner_id = api_user.id if api_user is not None else None
    item.is_private = bool(data.get('is_private', False))
    db.session.add(item)
    db.session.flush()  # assigns item.id so tag back-references work correctly
    assign_item_tags(item, data.get('tags'))
    recompute_item_privacy(item)
    item.slug = ensure_unique_slug(generate_slug(item.name), Item)
    db.session.commit()
    return jsonify(item_to_dict(item)), 201


@blueprint.route('/items/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_item(uuid):
    item = _get_item_or_404(uuid)
    user, sees_all = _api_viewer()
    visible_artefacts = (
        Artefact.query.join(Item, Artefact.item_id == Item.id)
        .filter(Artefact.item_id == item.id)
        .filter(artefact_visibility_clause(user, sees_all=sees_all))
        .all()
    )
    return jsonify(item_to_dict(item, include_artefacts=True, _artefacts=visible_artefacts))


@blueprint.route('/items/<string:uuid>', methods=['PUT'])
@require_auth('read_write')
def update_item(uuid):
    item = _get_item_or_404(uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    api_user, sees_all = _api_viewer()
    if item.private_effective and not can_contribute_to_item(item, api_user, sees_all=sees_all):
        return error_response('Not permitted to modify this item', 403)
    privacy_changed = False
    if 'name' in data:
        item.name = data['name']
    if 'description' in data:
        item.description = data['description']
    if 'platform_id' in data:
        item.platform_id = data['platform_id']
    if 'category_id' in data:
        item.category_id = data['category_id']
    if 'owner_id' in data:
        if not (sees_all or can_change_owner(item, api_user)):
            return error_response('Not permitted to change owner', 403)
        new_owner_id = data['owner_id']
        if new_owner_id is not None and db.session.get(User, new_owner_id) is None:
            return error_response('Owner user not found', 404)
        item.owner_id = new_owner_id
    if 'is_private' in data:
        if not (sees_all or can_curate_item(item, api_user, sees_all=sees_all)
                or can_manage_privacy(item, api_user)):
            return error_response('Not permitted to change privacy', 403)
        new_private = bool(data['is_private'])
        if new_private and can_claim_item(item, api_user):
            item.owner_id = api_user.id
        item.is_private = new_private
        privacy_changed = True
    if 'parent_uuid' in data:
        new_parent_id = None
        if data['parent_uuid'] is not None:
            new_parent = Item.query.filter(Item.uuid == data['parent_uuid']).first()
            if new_parent is None:
                return error_response('Parent item not found', 404)
            if not can_view_item(new_parent, api_user, sees_all=sees_all):
                return error_response('Parent item not found', 404)
            if new_parent.private_effective and not (sees_all or can_change_owner(new_parent, api_user)):
                return error_response('Not permitted to move items under this parent', 403)
            if new_parent.id == item.id or item.is_ancestor_of(new_parent):
                return error_response('Cannot move an item to itself or one of its descendants', 400)
            new_parent_id = new_parent.id
        if new_parent_id != item.parent_id and not (sees_all or can_change_owner(item, api_user)):
            return error_response('Not permitted to change parent', 403)
        item.parent_id = new_parent_id
        privacy_changed = True
    if 'name' in data:
        item.slug = ensure_unique_slug(generate_slug(item.name), Item, existing_id=item.id)
    if privacy_changed:
        db.session.flush()
        recompute_item_privacy(item)
    db.session.commit()
    return jsonify(item_to_dict(item))


@blueprint.route('/items/<string:uuid>', methods=['DELETE'])
@require_auth('read_write')
def delete_item(uuid):
    item = _get_item_or_404(uuid)
    api_user, sees_all = _api_viewer()
    if item.private_effective and not (sees_all or can_change_owner(item, api_user)):
        return error_response('Not permitted to delete this item', 403)
    bulk_delete_item(item)
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
    blob_storage_path = data.get('blob_storage_path', data['storage_path'])
    if not _validate_storage_path(blob_storage_path):
        return error_response('Invalid blob_storage_path')

    api_user, sees_all = _api_viewer()
    if item.private_effective and not can_contribute_to_item(item, api_user, sees_all=sees_all):
        return error_response('Not permitted to add artefacts to this item', 403)
    artefact = Artefact(item_id=item.id, label=data['label'], artefact_type=artefact_type,
                        description=data.get('description'), original_filename=data['original_filename'],
                        storage_path=data['storage_path'], storage_directory=storage_directory,
                        file_size=data.get('file_size'),
                        owner_id=(api_user.id if api_user is not None else item.owner_id),
                        is_private=bool(data.get('is_private', False)))
    blob, blob_created = assign_blob(
        artefact, storage_directory, blob_storage_path,
        data.get('file_size'), data.get('sha256'), data.get('md5'),
        logical_storage_path=data['storage_path'],
    )
    if (blob is not None and not blob_created
            and blob.storage_path != blob_storage_path):
        try:
            current_app.storage.delete(current_app.storage.storage_key(
                storage_directory.value, blob_storage_path
            ))
        except Exception:
            current_app.logger.warning(
                "Failed to remove duplicate registered object %s/%s",
                storage_directory.value, blob_storage_path,
            )
    db.session.add(artefact)
    db.session.flush()  # assigns artefact.id before slug generation
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
    api_user, sees_all = _api_viewer()
    if artefact.item.private_effective and not (sees_all or can_change_owner(artefact.item, api_user)):
        return error_response('Not permitted to delete artefacts from this item', 403)
    delete_artefact_files(artefact)
    db.session.delete(artefact)
    db.session.commit()
    return '', 204


@blueprint.route('/artefacts/<string:uuid>/move', methods=['POST'])
@require_auth('read_write')
def move_artefact(uuid):
    """Move a root artefact (and its derived artefacts) to a different item."""
    artefact = _get_artefact_or_404(uuid)
    api_user, sees_all = _api_viewer()
    data, error = _json_object()
    if error:
        return error

    target_uuid = data.get('target_item_uuid')
    if not target_uuid:
        return error_response('target_item_uuid is required', 400)

    target_item = Item.query.filter_by(uuid=target_uuid).first()
    if not target_item:
        return error_response('Target item not found', 404)
    if not can_view_item(target_item, api_user, sees_all=sees_all):
        return error_response('Target item not found', 404)

    try:
        validate_artefact_move(artefact, target_item, api_user, sees_all=sees_all)
    except ArtefactMoveError as e:
        status = 403 if e.code in ('source_forbidden', 'target_forbidden') else 400
        return error_response(str(e), status)

    try:
        move_artefact_to_item(artefact, target_item)
    except ValueError as e:
        return error_response(str(e), 400)

    return jsonify(artefact_to_dict(artefact))


@blueprint.route('/artefacts/<string:uuid>', methods=['PATCH'])
@require_auth('read_upload')
def update_artefact(uuid):
    """
    Update mutable fields on an artefact.

    Accepted fields:
      - ``md5`` / ``sha256`` — replace the stored hash strings.
      - ``media_metadata`` — JSON object that is **merged** (shallow, at the
        top-level key) into the existing ``media_metadata`` JSON, so different
        sections (e.g. ``iso9660``) can be written independently without
        clobbering one another.  Pass ``null`` to clear the field.
    """
    artefact = _get_artefact_or_404(uuid)
    api_user, sees_all = _api_viewer()
    if artefact.item.private_effective and not can_contribute_to_item(artefact.item, api_user, sees_all=sees_all):
        return error_response('Not permitted to modify artefacts in this item', 403)
    data, error = _json_object()
    if error:
        return error
    obsolete_storage_paths = []
    if 'md5' in data or 'sha256' in data:
        assign_blob(
            artefact,
            artefact.storage_directory,
            artefact_blob_storage_path(artefact),
            artefact.file_size,
            data.get('sha256', artefact.sha256),
            data.get('md5', artefact.md5),
            logical_storage_path=artefact.storage_path,
            obsolete_storage_paths=obsolete_storage_paths,
        )
    if 'artefact_type' in data and not artefact.type_overridden:
        try:
            artefact.artefact_type = ArtefactType(data['artefact_type'])
        except ValueError:
            return error_response(f"Unknown artefact_type: {data['artefact_type']}", 400)
    if 'media_metadata' in data:
        incoming = data['media_metadata']
        if incoming is None:
            artefact.media_metadata = None
        elif isinstance(incoming, dict):
            existing = {}
            if artefact.media_metadata:
                try:
                    parsed = json.loads(artefact.media_metadata)
                    if isinstance(parsed, dict):
                        existing = parsed
                except (json.JSONDecodeError, TypeError):
                    existing = {}
            existing.update(incoming)
            artefact.media_metadata = json.dumps(existing)
        else:
            return error_response('media_metadata must be an object or null', 400)
    db.session.commit()
    for storage_path in obsolete_storage_paths:
        try:
            current_app.storage.delete(current_app.storage.storage_key(
                artefact.storage_directory.value, storage_path
            ))
        except Exception:
            current_app.logger.warning(
                "Failed to remove obsolete blob object %s/%s",
                artefact.storage_directory.value,
                storage_path,
            )
    return jsonify(artefact_to_dict(artefact))


@blueprint.route('/artefacts/<string:uuid>/transform-to-disk-image', methods=['POST'])
@require_auth('read_upload')
def transform_to_disk_image(uuid):
    """Replace a disk-image-bundle ZIP artefact's stored file with its image.

    Worker-only.  The worker has already uploaded the extracted (compressed)
    disk image to ``storage_path`` under ``uploads/``; this repoints the artefact
    at it (type → compressed raw-sector), drops the old zip object from storage,
    and queues the disk-image analyses.  The artefact stays top-level so its
    UUID/URL are unchanged.  Idempotent: a retry that finds the artefact already
    transformed returns the artefact unchanged.
    """
    if not _is_worker_request():
        return error_response('Only the worker may transform artefacts', 403)
    artefact = _get_artefact_or_404(uuid)
    data, error = _json_object(required=True)
    if error:
        return error
    missing = _require_fields(data, 'storage_path', 'original_filename', 'artefact_type')
    if missing:
        return missing
    if not _validate_storage_path(data['storage_path']):
        return error_response('Invalid storage_path')
    try:
        new_type = ArtefactType(data['artefact_type'])
    except ValueError:
        return error_response('Invalid artefact_type')
    if new_type not in COMPRESSED_RAW_SECTOR_TYPES:
        return error_response('transform target must be a compressed raw-sector type', 400)

    # Idempotency: a worker retry after the row was already switched is a no-op.
    if (artefact.storage_path == data['storage_path']
            and artefact.storage_directory == StorageDirectory.UPLOADS):
        return jsonify(artefact_to_dict(artefact))

    old_blob = artefact_blob(artefact)
    old_key = get_artefact_storage_key(artefact)

    artefact.original_filename = data['original_filename']
    # Only overwrite size/hashes when supplied, so an omitted field never nulls
    # out integrity metadata (used by dedup and hash-DB linking).
    if 'mime_type' in data:
        artefact.mime_type = data['mime_type']
    new_blob, new_blob_created = assign_blob(
        artefact,
        StorageDirectory.UPLOADS,
        data['storage_path'],
        data.get('file_size', artefact.file_size),
        data.get('sha256', artefact.sha256),
        data.get('md5', artefact.md5),
    )
    if (new_blob is not None and not new_blob_created
            and new_blob.storage_path != data['storage_path']):
        try:
            current_app.storage.delete(current_app.storage.storage_key(
                'uploads', data['storage_path']
            ))
        except Exception:
            current_app.logger.warning(
                "Failed to remove duplicate transformed object uploads/%s",
                data["storage_path"],
            )
    # The stored bytes are now a disk image, so the type MUST reflect that — even
    # if a ZIP type was manually pinned, that pin described content that no longer
    # exists, and leaving it would re-queue ARCHIVE_EXTRACT against a raw image.
    artefact.artefact_type = new_type

    db.session.flush()

    # Drop the old object only when this artefact held its final reference.
    if old_blob is None or len(old_blob.artefacts) == 0:
        try:
            current_app.storage.delete(old_key)
            if old_blob is not None:
                db.session.delete(old_blob)
        except Exception as e:
            current_app.logger.warning(
                f"transform_to_disk_image: failed to delete old object {old_key}: {e}"
            )

    # When the worker supplied the image's hashes, skip the redundant
    # CHECKSUM_COMPUTE that would re-read the whole image just to recompute them;
    # otherwise let it run so the hashes are correct.
    skip = ['CHECKSUM_COMPUTE'] if ('md5' in data and 'sha256' in data) else []
    queue_analyses_for_artefact(artefact, commit=False, skip_analyses=skip)
    db.session.commit()
    return jsonify(artefact_to_dict(artefact))


@blueprint.route('/artefacts/<string:uuid>/download', methods=['GET'])
@require_auth('read_only')
def download_artefact(uuid):
    artefact = _get_artefact_or_404(uuid, selectinload(Artefact.restrictions))

    # Enforce download restrictions, honouring the caller's bypass grants.
    # Restrictions on a container artefact cascade to artefacts derived from it.
    user, _ = _api_viewer()
    restrictions = artefact.effective_restrictions
    if not can_download_despite_restrictions(user, restrictions, artefact):
        return error_response('Download restricted', 403,
                              restrictions=list({r.restriction_type.value for r in restrictions}))

    # Block the original download when its extracted contents are restricted
    # (mirrors the website's _check_artefact_file_restrictions).
    contained = artefact_contained_file_restrictions(artefact)
    if not can_download_despite_restrictions(user, contained, artefact):
        return error_response('Download restricted (artefact contains restricted files)', 403,
                              restrictions=list({r.restriction_type.value for r in contained}))

    response = serve_artefact_file(artefact)
    if response is None:
        return error_response('File not found', 404)
    return response


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
    _require_view_artefact(artefact)

    # Restriction gate, honouring the caller's bypass grants (same policy as web).
    # Restrictions on a container artefact cascade to artefacts derived from it.
    user, _ = _api_viewer()
    restrictions = artefact.effective_restrictions
    if not can_download_despite_restrictions(user, restrictions, artefact):
        return error_response('Download restricted', 403,
                              restrictions=list({r.restriction_type.value for r in restrictions}))

    # Check file-level restrictions: own, descendants (archive contains restricted file),
    # and ancestors (file is inside a restricted archive).
    file_restrictions = (
        collect_all_file_restrictions(ef) + collect_ancestor_file_restrictions(ef)
    )
    if not can_download_despite_restrictions(user, file_restrictions, artefact):
        return error_response('File download restricted', 403,
                              restrictions=list({r.restriction_type.value for r in file_restrictions}))

    response = serve_extracted_file(ef)
    if response is None:
        return error_response('Extracted file not found on disk', 404)
    return response


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

    hints_json = data.get('hints')

    # Acquire an exclusive row lock on the artefact before the idempotency
    # check + insert.  Without this, two workers finishing sibling analyses
    # simultaneously can both pass the "no existing PENDING/RUNNING" check
    # and both insert duplicate jobs for the same follow-on analysis type.
    # The lock serialises concurrent request_analysis calls for the same
    # artefact, ensuring only one insert succeeds per (type, hints) pair.
    db.session.execute(
        select(Artefact).where(Artefact.id == artefact.id).with_for_update()
    )

    # Idempotency: return existing PENDING/RUNNING analysis instead of creating a
    # duplicate.  COMPLETED/FAILED analyses may be intentionally re-run, so only
    # active ones are considered.  This mirrors the logic in queue_analyses_for_artefact().
    # Include hints in the check so that multiple ARCHIVE_EXTRACT jobs for
    # different files within the same artefact are not collapsed into one.
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


@blueprint.route('/artefacts/<string:uuid>/analysis/tree', methods=['GET'])
@require_auth('read_only')
def get_artefact_analysis_tree(uuid):
    """Return the full derivation tree for an artefact.

    Recursively walks: artefact -> analyses -> produced_artefacts -> analyses -> ...
    """
    from ..utils.api_serializers import analysis_tree_node
    artefact = _get_artefact_or_404(uuid)
    return jsonify({'artefact': analysis_tree_node(artefact)})


@blueprint.route('/artefacts/<string:uuid>/processing-tree', methods=['GET'])
@require_auth('read_only')
def get_artefact_processing_tree(uuid):
    """Return the full processing tree for an artefact.

    Always navigates to the root artefact of the given UUID.  Returns a
    nested structure grouping analyses by artefact, with path-bearing
    analyses (archive extract, format convert, etc.) grouped under their
    file-path context.
    """
    from ..utils.api_serializers import processing_tree_to_dict
    artefact = _get_artefact_or_404(uuid)
    root = artefact.root_artefact
    return jsonify(processing_tree_to_dict(root))


@blueprint.route('/artefacts/<string:uuid>/analysis/recursive', methods=['GET'])
@require_auth('read_only')
def get_artefact_analyses_recursive(uuid):
    """Return all analyses for an artefact and all its descendants (flat list).

    Optional query param: ?status=failed to filter by status.
    """
    artefact = _get_artefact_or_404(uuid)
    analyses = collect_all_analyses(artefact)

    status_filter = request.args.get('status')
    if status_filter:
        try:
            target_status = AnalysisStatus(status_filter)
        except ValueError:
            return error_response(f'Invalid status: {status_filter}')
        analyses = [a for a in analyses if a.status == target_status]

    total = len(analyses)
    failed = sum(1 for a in analyses if a.status == AnalysisStatus.FAILED)

    return jsonify({
        'artefact_uuid': artefact.uuid,
        'artefact_label': artefact.label,
        'analyses': [analysis_to_dict(a, include_artefact=True) for a in analyses],
        'total': total,
        'failed': failed,
    })


@blueprint.route('/analysis/failures', methods=['GET'])
@require_auth('read_only')
def search_failed_analyses():
    """Search failed analyses system-wide with optional filters.

    Query params: analysis_type, tool_name, since, until, error, page, per_page.
    """
    _user, _sees_all = _api_viewer()
    query = (
        Analysis.query
        .join(Artefact, Analysis.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(Analysis.status == AnalysisStatus.FAILED)
        .filter(artefact_visibility_clause(_user, sees_all=_sees_all))
    )

    analysis_type = request.args.get('analysis_type')
    if analysis_type:
        try:
            at = AnalysisType(analysis_type)
        except ValueError:
            return error_response(f'Invalid analysis_type: {analysis_type}')
        query = query.filter(Analysis.analysis_type == at)

    tool_name = request.args.get('tool_name')
    if tool_name:
        query = query.filter(Analysis.tool_name == tool_name)

    since = request.args.get('since')
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            return error_response(f'Invalid since date: {since}')
        query = query.filter(Analysis.completed_at >= since_dt)

    until = request.args.get('until')
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
        except ValueError:
            return error_response(f'Invalid until date: {until}')
        query = query.filter(Analysis.completed_at <= until_dt)

    error_pattern = request.args.get('error')
    if error_pattern:
        query = query.filter(Analysis.error_message.ilike(f'%{error_pattern}%'))

    # Re-use the explicit join above for eager loading rather than emitting
    # a second pair of aliased joins via joinedload().
    query = query.options(
        contains_eager(Analysis.artefact).contains_eager(Artefact.item)
    ).order_by(Analysis.completed_at.desc())

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    offset = (page - 1) * per_page

    total = query.count()
    analyses = query.offset(offset).limit(per_page).all()

    return jsonify({
        'failures': [analysis_to_dict(a, include_artefact=True) for a in analyses],
        'total': total,
        'page': page,
        'per_page': per_page,
    })


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
            .values(status=AnalysisStatus.RUNNING, started_at=datetime.now(timezone.utc).replace(tzinfo=None))
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
        analysis.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if data.get('status') in ('completed', 'failed'):
        analysis.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)

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
        return error_response('Analysis was deleted during update', 404)
    return jsonify(analysis_to_dict(analysis))


def _populate_search_index(analysis):
    from ..services.search_index import populate_search_index_from_analysis
    populate_search_index_from_analysis(analysis)


@blueprint.route('/analysis/pending', methods=['GET'])
@require_auth('read_only')
def get_pending_analyses():
    """Get pending analyses (for worker).

    Optional query parameter:
        types: comma-separated AnalysisType names to restrict results to
               (e.g. ``?types=FLUX_VISUALISATION,FLUX_DECODE``).
               Unknown names are silently ignored.
    """
    # Worker-only: this is the worker's job-polling endpoint and returns
    # storage paths plus metadata for *every* pending analysis system-wide,
    # with no per-artefact visibility filter.  An ordinary user API key must
    # not be able to enumerate private artefacts (uuids, item slugs, storage
    # locations) through it.  The worker authenticates with the pre-shared key.
    if not _is_worker_request():
        return error_response('Only the worker may poll pending analyses', 403)

    query = (
        Analysis.query
        .filter(Analysis.status == AnalysisStatus.PENDING)
        .options(joinedload(Analysis.artefact).joinedload(Artefact.item))
    )
    # Re-analysis dispatch barrier.  A re-analysis queues a CLEANUP job (delete
    # the previous run's output) alongside the replacement analyses.  While that
    # CLEANUP is still outstanding (PENDING or RUNNING), do NOT hand out any
    # other analysis for the same artefact: otherwise a second worker could run
    # a replacement job concurrently and the CLEANUP would then delete its
    # output — notably the shared outputs/.cache/<uuid> partition cache, which
    # is keyed on the artefact (not the analysis) and repopulated by the new
    # run.  The CLEANUP row itself is never blocked (so it gets claimed), and
    # item-deletion cleanups (artefact_id IS NULL) gate nothing.  The barrier
    # keys on PENDING/RUNNING only, so a terminal CLEANUP (COMPLETED or FAILED,
    # incl. malformed-hints failures) lifts it — a best-effort cleanup never
    # deadlocks the new run; a crash leaves it RUNNING until reset-stale
    # re-queues it, which holds the artefact's jobs in the safe direction.
    blocking_cleanup_artefacts = (
        select(Analysis.artefact_id)
        .where(
            Analysis.analysis_type == AnalysisType.CLEANUP,
            Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING]),
            Analysis.artefact_id.isnot(None),
        )
    )
    query = query.filter(or_(
        Analysis.analysis_type == AnalysisType.CLEANUP,
        Analysis.artefact_id.is_(None),
        Analysis.artefact_id.notin_(blocking_cleanup_artefacts),
    ))
    types_param = request.args.get('types', '')
    if types_param:
        requested_names = [t.strip() for t in types_param.split(',') if t.strip()]
        valid_types = [AnalysisType[n] for n in requested_names if n in AnalysisType.__members__]
        if valid_types:
            query = query.filter(Analysis.analysis_type.in_(valid_types))
    analyses = (
        query
        .order_by(Analysis.priority.desc(), Analysis.created_at)
        .limit(50)
        .all()
    )
    # Worker reads storage_path / storage_directory from each artefact to
    # locate the input file (see worker/arcworker/analysis.py).
    return jsonify({'analyses': [
        analysis_to_dict(a, include_artefact=True, include_artefact_storage=True)
        for a in analyses
    ]})


@blueprint.route('/analysis/reset-stale', methods=['POST'])
@require_auth('read_write')
def reset_stale_analyses():
    """Reset RUNNING jobs stuck longer than the stale timeout back to PENDING.

    Called by the worker on startup to recover any jobs left in RUNNING state
    by a previous worker crash.  Also callable by operators via the UI.
    """
    # This re-queues every stale RUNNING job system-wide, including jobs on
    # private artefacts the caller cannot see.  Restrict to the worker (startup
    # crash recovery) or a staff+ operator — an ordinary read_write user must
    # not be able to disrupt the global analysis queue.  Mirrors the staff gate
    # on the UI route (blueprints/analysis.py reset_stale).
    if not _is_worker_request():
        user = getattr(g, 'api_user', None)
        if user is None or not (
                getattr(user, 'is_admin', False) or user.has_permission(UserPermission.STAFF)):
            return error_response('Staff permission required', 403)

    timeout_seconds = current_app.config.get('STALE_JOB_TIMEOUT_SECONDS', 3600)
    # started_at is stored as naive UTC
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=timeout_seconds)
    # Use a single atomic UPDATE rather than a read-modify-write loop.  A
    # non-atomic approach (query.all() then modify in memory then commit) has a
    # race window where a worker can complete and commit status='completed'
    # between the read and the commit, causing the reset to overwrite the
    # completion and re-queue an already-finished job.
    result = db.session.execute(
        update(Analysis)
        .where(Analysis.status == AnalysisStatus.RUNNING)
        .where(Analysis.started_at < cutoff)
        .values(
            status=AnalysisStatus.PENDING,
            error_message=None,
            started_at=None,
            completed_at=None,
            tool_name=None,
            tool_version=None,
            output_url=None,
            output_path=None,
            success=None,
            summary=None,
            details=None,
        )
    )
    db.session.commit()
    count = result.rowcount
    if count:
        current_app.logger.info(f'Reset {count} stale analysis job(s) to PENDING')
    return jsonify({'reset': count})


# =============================================================================
# Output Files
# =============================================================================

@blueprint.route('/outputs/<path:filename>', methods=['GET'])
@require_auth('read_only')
def get_output_file(filename):
    """Serve an output file (visualisation, etc.).

    Enforces artefact visibility so a low-privilege user API key cannot pull
    the outputs (visualisations, extracted text, file listings) of a private
    artefact just by knowing the path.  The worker authenticates with the
    pre-shared key and sees everything via _api_viewer()'s sees_all flag.
    Serving is delegated to the shared downloads service (same code path as
    the web endpoint).
    """
    artefact_for_check = resolve_output_artefact(filename)
    if artefact_for_check is None:
        return error_response('File not found', 404)
    user, sees_all = _api_viewer()
    if not can_view_artefact(artefact_for_check, user, sees_all=sees_all):
        return error_response('File not found', 404)

    # Download restrictions gate the original bytes; analysis outputs render the
    # same content, so a caller who cannot bypass the artefact's restrictions
    # must not read its outputs either.  This mirrors download_artefact() — the
    # worker key (user is None) is intentionally blocked from restricted bytes.
    if output_blocked_for(user, artefact_for_check):
        return error_response('Download restricted', 403)

    response = serve_output_file(filename)
    if response is None:
        return error_response('File not found', 404)
    return response


# =============================================================================
# Partitions & Files
# =============================================================================

def _merge_produce_hints(analysis, data):
    """Build the hints dict for follow-on analyses of a derived artefact.

    Starts from the parent analysis's hints (shared by all siblings) and
    overlays any per-artefact hints supplied in the produce-artefact payload.
    Per-artefact hints win on conflict.  Returns None when there are no hints.
    """
    hints = json.loads(analysis.hints) if analysis.hints else {}
    artefact_hints = data.get('hints')
    if isinstance(artefact_hints, dict):
        hints = {**hints, **artefact_hints}
    return hints or None


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
    blob_storage_path = data.get('blob_storage_path', data['storage_path'])
    if not _validate_storage_path(blob_storage_path):
        return error_response('Invalid blob_storage_path')

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
        # mid-loop when lazy-loading derived_artefacts inside delete_artefact_files.
        with db.session.no_autoflush:
            # Pre-collect every ID being deleted (across all priors and their
            # subtrees) so _delete_artefact_files treats siblings as "being
            # deleted" and does not skip a shared blob because a sibling that
            # hasn't been flushed yet appears as an external reference.
            all_prior_ids: set[int] = set()
            stack = list(prior_derived)
            while stack:
                node = stack.pop()
                all_prior_ids.add(node.id)
                stack.extend(node.derived_artefacts)
            processed_blobs: set = set()
            for prior in prior_derived:
                current_app.logger.info(
                    f"Removing prior derived artefact {prior.uuid} "
                    f"(from previous {enum_value(analysis.analysis_type)} analysis) "
                    f"before re-run"
                )
                delete_artefact_files(prior, deleting_ids=all_prior_ids,
                                      processed_blobs=processed_blobs)
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
        parent_artefact_id=analysis.artefact_id,
        derived_from_analysis_id=analysis.id,
        # Derived artefacts inherit the source artefact's owner and privacy flag.
        # Item-level privacy descends automatically via Item.private_effective, but
        # artefact-level is_private must be copied explicitly to avoid exposing a
        # private artefact's derived outputs when the parent item is public.
        owner_id=analysis.artefact.owner_id,
        is_private=analysis.artefact.is_private,
    )
    blob, blob_created = assign_blob(
        artefact, storage_directory, blob_storage_path,
        data.get('file_size'), data.get('sha256'), data.get('md5'),
        logical_storage_path=data['storage_path'],
    )
    if (blob is not None and not blob_created
            and blob.storage_path != blob_storage_path):
        try:
            current_app.storage.delete(current_app.storage.storage_key(
                storage_directory.value, blob_storage_path
            ))
        except Exception:
            current_app.logger.warning(
                "Failed to remove duplicate derived object %s/%s",
                storage_directory.value, blob_storage_path,
            )
    
    db.session.add(artefact)
    try:
        db.session.flush()  # assigns artefact.id before slug generation
    except IntegrityError:
        db.session.rollback()
        existing = Artefact.query.filter_by(
            derived_from_analysis_id=analysis.id,
            storage_path=data["storage_path"],
        ).first()
        if not existing:
            return error_response('Failed to create derived artefact (integrity error)', 500)
        # Re-queue analyses on the artefact already registered for this analysis/path.
        # queue_analyses_for_artefact skips PENDING/RUNNING duplicates, so this
        # is safe to call even if some analyses are already active.
        queued_analyses = []
        if data.get('auto_analyse', True):
            from ..services.artefact_types import ANALYSIS_MAP
            hints = _merge_produce_hints(analysis, data)
            skip_analyses = data.get('skip_analyses') or []
            queue_analyses_for_artefact(existing, hints, skip_analyses=skip_analyses)
            skip_set = set(skip_analyses)
            queued_analyses = [
                t.value for t in ANALYSIS_MAP.get(artefact_type, [])
                if t.name not in skip_set
            ]

        return jsonify({
            'artefact': artefact_to_dict(existing),
            'queued_analyses': queued_analyses,
        }), 200

    # Generate slug and commit artefact atomically.
    artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': analysis.artefact.item_id})
    db.session.commit()

    # Queue follow-on analyses unless the caller will handle that explicitly
    queued_analyses = []
    if data.get('auto_analyse', True):
        from ..services.artefact_types import ANALYSIS_MAP
        hints = _merge_produce_hints(analysis, data)
        skip_analyses = data.get('skip_analyses') or []
        queue_analyses_for_artefact(artefact, hints, skip_analyses=skip_analyses)
        skip_set = set(skip_analyses)
        queued_analyses = [t.value for t in ANALYSIS_MAP.get(artefact_type, []) if t.name not in skip_set]

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
                          archive_comment=data.get('archive_comment'),
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

    # Acquire an exclusive row lock on the parent artefact before any writes.
    # This serialises concurrent add_files() calls for different partitions of
    # the same artefact, preventing the deadlock that arises when two workers
    # concurrently UPDATE partitions and both trigger a FK CHECK (FOR KEY SHARE)
    # on the same artefacts row in a conflicting order.
    db.session.execute(
        select(Artefact).where(Artefact.id == partition.artefact_id).with_for_update()
    )

    data, error = _json_object(required=True)
    if error:
        return error
    if 'files' not in data:
        return error_response('files array required')

    # Resolve the final (possibly archive-nested) path for each incoming file
    # up front so duplicate detection and known-file matching can both run as
    # single batch queries instead of one query per file.  A partition listing
    # can contain thousands of files; the per-file N+1 pattern here was slow
    # enough under concurrent extraction to blow the worker's HTTP timeout.
    candidates = []
    for f in data['files']:
        if 'path' not in f or 'filename' not in f:
            continue

        # If this file was extracted from an archive (has parent_file_id),
        # nest it under the parent archive's path so the archive appears
        # as a virtual directory in the UI.
        path = f['path']
        parent_file_id = f.get('parent_file_id')
        if parent_file_id:
            parent_file = db.session.get(ExtractedFile, parent_file_id)
            if parent_file and parent_file.is_archive:
                if not path.startswith(parent_file.path + '/'):
                    path = parent_file.path + '/' + path

        candidates.append((path, f))

    # Single query for all paths already present in this partition (duplicate
    # guard), instead of one SELECT per incoming file.
    existing_paths = set()
    if candidates:
        incoming_paths = {path for path, _ in candidates}
        existing_paths = {
            row[0]
            for row in db.session.query(ExtractedFile.path)
            .filter(
                ExtractedFile.partition_id == partition.id,
                ExtractedFile.path.in_(incoming_paths),
            )
            .all()
        }

    # Single batch query to match every incoming file against active hash
    # databases, replacing the per-file find_known_file() N+1 pattern.
    known_matches = find_known_files_for_records([f for _, f in candidates])

    added = 0
    added_unique = 0
    skipped = 0
    seen_paths = set()
    for (path, f), known in zip(candidates, known_matches, strict=True):
        # Guard against both pre-existing rows and duplicates within this batch.
        if path in existing_paths or path in seen_paths:
            skipped += 1
            continue
        seen_paths.add(path)

        modified_time = None
        modified_time_str = f.get('modified_time')
        if modified_time_str:
            try:
                modified_time = datetime.fromisoformat(modified_time_str)
            except (ValueError, TypeError):
                pass

        ef = ExtractedFile(
            partition_id=partition.id,
            path=path,
            filename=f['filename'],
            extension=f.get('extension'),
            file_size=f.get('file_size'),
            modified_time=modified_time,
            md5=f.get('md5'),
            sha1=f.get('sha1'),
            sha256=f.get('sha256'),
            # Archive support fields
            is_directory=f.get('is_directory', False),
            risc_os_filetype=f.get('risc_os_filetype'),
            load_address=f.get('load_address'),
            exec_address=f.get('exec_address'),
            attributes=f.get('attributes'),
            parent_file_id=f.get('parent_file_id'),
            extraction_depth=f.get('extraction_depth', 0)
        )

        if known is not None:
            ef.known_file_id = known.id
            ef.is_known = True
        else:
            added_unique += 1
        db.session.add(ef)
        added += 1

    # Update the partition counters incrementally rather than re-running two
    # COUNT(*) scans over the (growing) partition on every batch.  A large disc
    # is registered as many sequential 100-file batches; the per-batch full
    # counts made each request progressively slower and, under concurrent
    # extraction, helped saturate the sync worker pool until other workers'
    # requests timed out.  Duplicates are already filtered above, so the row
    # delta for this batch is exactly `added` (of which `added_unique` are not
    # known-file matches).
    partition.total_files = (partition.total_files or 0) + added
    partition.unique_files = (partition.unique_files or 0) + added_unique
    try:
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        # pgcode '40P01' is DeadlockDetected — return 503 so the worker can
        # retry rather than waiting for the stale-job timeout to fire.
        if getattr(exc.orig, 'pgcode', None) == '40P01':
            return error_response('Deadlock detected, please retry', 503)
        raise

    # Auto-apply restrictions from flagged hash databases.  This is a no-op
    # unless some active hash database has a restriction_type set, but the full
    # implementation re-scans every known file across all of the artefact's
    # partitions — O(files) work that, repeated per batch, becomes O(files²)
    # over a large disc.  Gate it behind a single cheap existence check so the
    # common case (no flagged databases) costs one indexed lookup instead.
    if added > 0 and _has_restricting_hash_database():
        from ..services.hash_rescan import apply_database_restrictions
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

    # Narrow to a specific archive extraction context (pushed down from worker
    # to avoid transferring the entire partition just to filter in Python).
    path_prefix_param = request.args.get('path_prefix')
    extraction_depth_param = request.args.get('extraction_depth', type=int)
    if path_prefix_param is not None:
        query = query.filter(ExtractedFile.path.startswith(path_prefix_param + '/'))
    if extraction_depth_param is not None:
        query = query.filter(ExtractedFile.extraction_depth == extraction_depth_param)

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
    if 'archive_format' in data:
        file.archive_format = data.get('archive_format')
    if 'archive_comment' in data:
        file.archive_comment = data.get('archive_comment')

    db.session.commit()
    return jsonify(file_to_dict(file))


@blueprint.route('/files/<int:file_id>', methods=['GET'])
@require_auth('read_only')
def get_extracted_file(file_id):
    if not _is_worker_request():
        return error_response('Only the worker may access files by integer ID', 403)
    file = _get_extracted_file_or_404(id=file_id)
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

    _require_view_item(ref.item)
    user, sees_all = _api_viewer()
    visible_artefacts = (
        Artefact.query.join(Item, Artefact.item_id == Item.id)
        .filter(Artefact.item_id == ref.item.id)
        .filter(artefact_visibility_clause(user, sees_all=sees_all))
        .all()
    )
    return jsonify(item_to_dict(ref.item, include_artefacts=True, _artefacts=visible_artefacts))


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
    
    _user, _sees_all = _api_viewer()
    extracted = query.options(
        joinedload(ExtractedFile.partition).joinedload(Partition.artefact).joinedload(Artefact.item)
    ).join(Partition, ExtractedFile.partition_id == Partition.id) \
     .join(Artefact, Partition.artefact_id == Artefact.id) \
     .join(Item, Artefact.item_id == Item.id) \
     .filter(artefact_visibility_clause(_user, sees_all=_sees_all)) \
     .all()
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
	  - is_private: whether the new artefact is private (optional, default false)
	  - auto_analyse: queue automatic analyses (optional, default 'true')

	Returns the created artefact JSON with 201 status.
	"""
	item = _get_item_or_404(item_uuid)
	manage_error = _require_manage_item_content(item)
	if manage_error:
		return manage_error

	if 'file' not in request.files:
		return error_response('No file provided')
	file = request.files['file']
	if file.filename == '':
		return error_response('No file selected')

	label = request.form.get('label')
	if not label:
		return error_response('Label is required')

	# Save file with UUID-based name
	try:
		storage_name, file_size = save_uploaded_file(file)
	except OSError as exc:
		return error_response(f'Storage backend unavailable: {exc}', 503)

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

	# Compute hashes via storage backend
	storage_key = current_app.storage.storage_key('uploads', storage_name)
	try:
		md5, sha256 = compute_file_hashes(storage_key, use_storage=True)
	except OSError as exc:
		return error_response(f'Storage backend unavailable (hash): {exc}', 503)

	# Preserve original filename
	from werkzeug.utils import secure_filename
	original_filename = secure_filename(file.filename) or 'unnamed'

	# Parse optional hints (JSON string from form field) before creating
	# anything, so an invalid request leaves no artefact behind.
	hints = None
	hints_raw = request.form.get('hints')
	if hints_raw:
		try:
			hints = json.loads(hints_raw)
			if not isinstance(hints, dict):
				return error_response('hints must be a JSON object')
		except json.JSONDecodeError:
			return error_response('hints must be valid JSON')

	auto_analyse = request.form.get('auto_analyse', 'true').lower() != 'false'

	# Duplicate check, artefact + slug + analysis queue (single transaction),
	# and orphan-file cleanup on failure all happen in the shared pipeline.
	outcome = ingest_uploaded_artefact(
		item,
		label=label,
		artefact_type=artefact_type,
		type_overridden=type_overridden,
		original_filename=original_filename,
		storage_name=storage_name,
		file_size=file_size,
		md5=md5,
		sha256=sha256,
		description=request.form.get('description'),
		hints=hints,
		queue=QUEUE_FULL if auto_analyse else QUEUE_NONE,
	)
	result = artefact_to_dict(outcome.artefact)
	result['queued_analyses'] = [
		t.value for t in outcome.queued_analyses
		if t != AnalysisType.CHECKSUM_COMPUTE
	]
	return jsonify(result), 201


# =============================================================================
# Chunked Upload
# =============================================================================
#
# Protocol:
#   POST /api/uploads/chunked/init                              → {upload_uuid}
#   POST /api/uploads/chunked/<upload_uuid>/chunk/<chunk_index> → {received, chunk}
#   GET  /api/uploads/chunked/<upload_uuid>/status              → {received_chunks, total_chunks}
#   POST /api/uploads/chunked/<upload_uuid>/complete            → (same 201 JSON as /artefacts/upload)
#
# Chunks are stored locally under <instance_path>/.chunks/<upload_uuid>/ regardless
# of the active storage backend (local or S3).  On /complete they are assembled
# into a tempfile and pushed via storage.put(), mirroring save_uploaded_file().

# The chunk storage/assembly mechanics live in services/chunked_upload.py so the
# web blueprint (session-authenticated browser uploads) shares one implementation.


def _load_chunk_meta(upload_uuid):
    """Load a chunk session's meta, enforcing existence and caller ownership.

    Returns (meta, None) or (None, error_response).  Sessions are bound to their
    creating user (``creator_user_id`` in meta); the worker key never creates
    sessions, so a session with no recorded creator imposes no extra constraint.
    A session owned by someone else is reported as 404 so it is
    indistinguishable from a nonexistent one.
    """
    try:
        meta = _chunked.read_meta(upload_uuid)
    except _chunked.ChunkSessionCorrupt:
        return None, error_response('Upload session corrupt', 500)
    if meta is None:
        return None, error_response('Upload session not found', 404)
    creator_id = meta.get('creator_user_id')
    if creator_id is not None:
        user = getattr(g, 'api_user', None)
        if user is None or user.id != creator_id:
            return None, error_response('Upload session not found', 404)
    return meta, None


@blueprint.route('/uploads/chunked/init', methods=['POST'])
@require_auth('read_upload')
def chunked_upload_init():
	"""
	Initialise a chunked upload session.

	Accepts JSON with:
	  - filename: original filename (required)
	  - total_chunks: number of chunks the client will send (required)
	  - item_uuid: target item (required)
	  - label: artefact display label (required)
	  - total_size: total file size in bytes (optional)
	  - artefact_type: override auto-detection (optional, default 'auto')
	  - description: artefact description (optional)
	  - auto_analyse: queue automatic analyses (optional, default true)

	Returns {"upload_uuid": "<hex>"}.
	"""
	data = request.get_json(silent=True) or {}

	filename = data.get('filename', '').strip()
	if not filename:
		return error_response('filename is required')

	try:
		total_chunks = int(data['total_chunks'])
		if total_chunks < 1:
			raise ValueError
	except (KeyError, ValueError, TypeError):
		return error_response('total_chunks must be a positive integer')
	if total_chunks > _chunked.MAX_TOTAL_CHUNKS:
		return error_response(
			f'total_chunks exceeds the maximum of {_chunked.MAX_TOTAL_CHUNKS}')

	# Reject up front when the client already declares an over-size upload.
	# The assembled size is re-checked authoritatively in /complete.
	max_size = _chunked.max_upload_size()
	total_size = data.get('total_size')
	if total_size is not None:
		try:
			if int(total_size) > max_size:
				return error_response(
					f'Upload exceeds the maximum size of {max_size} bytes', 413)
		except (ValueError, TypeError):
			return error_response('total_size must be an integer')

	item_uuid = data.get('item_uuid', '')
	if not item_uuid:
		return error_response('item_uuid is required')
	# Validate item exists
	item = _get_item_or_404(item_uuid)
	manage_error = _require_manage_item_content(item)
	if manage_error:
		return manage_error

	label = data.get('label', '').strip()
	if not label:
		return error_response('label is required')

	hints = data.get('hints')
	if hints is not None and not isinstance(hints, dict):
		return error_response('hints must be a JSON object')

	creator = getattr(g, 'api_user', None)
	upload_uuid = _chunked.init_chunk_session({
		'filename': filename,
		'total_chunks': total_chunks,
		'total_size': data.get('total_size'),
		'item_uuid': item_uuid,
		'label': label,
		'artefact_type': data.get('artefact_type', 'auto'),
		'description': data.get('description'),
		'is_private': bool(data.get('is_private', False)),
		'auto_analyse': data.get('auto_analyse', True),
		'hints': hints,
		'creator_user_id': creator.id if creator is not None else None,
		'created_at': datetime.now(timezone.utc).isoformat(),
	})
	return jsonify({'upload_uuid': upload_uuid}), 201


@blueprint.route('/uploads/chunked/<string:upload_uuid>/chunk/<int:chunk_index>', methods=['POST'])
@require_auth('read_upload')
def chunked_upload_chunk(upload_uuid, chunk_index):
	"""
	Upload a single chunk.

	The request body must be raw binary (application/octet-stream).
	chunk_index is zero-based.

	Returns {"received": true, "chunk": N}.
	"""
	meta, error = _load_chunk_meta(upload_uuid)
	if error:
		return error

	# Once finalise has been claimed the session is immutable; reject late chunk
	# writes rather than corrupting an in-progress or completed assembly.
	if meta.get('finalize_state') is not None:
		return error_response('Upload is being finalised; no more chunks accepted', 409)

	# chunk_index is a non-negative int (route converter); reject out-of-range
	# indices so a caller cannot scatter junk chunk files that never assemble.
	if chunk_index < 0 or chunk_index >= meta['total_chunks']:
		return error_response('chunk_index out of range', 400)

	_chunked.write_chunk(upload_uuid, chunk_index, request.data)
	return jsonify({'received': True, 'chunk': chunk_index})


@blueprint.route('/uploads/chunked/<string:upload_uuid>/status', methods=['GET'])
@require_auth('read_upload')
def chunked_upload_status(upload_uuid):
	"""
	Query which chunks have been received.

	Returns {"upload_uuid": "...", "total_chunks": N, "received_chunks": [0, 1, ...]}.
	Allows clients to resume after a partial failure.
	"""
	meta, error = _load_chunk_meta(upload_uuid)
	if error:
		return error

	return jsonify({
		'upload_uuid': upload_uuid,
		'total_chunks': meta['total_chunks'],
		'received_chunks': _chunked.received_chunks(upload_uuid),
	})


def _resolve_chunk_artefact_type(meta):
	"""Resolve (artefact_type, type_overridden, original_filename) from meta.

	Returns ((…tuple…), None) or (None, error_response) when the stored
	artefact_type override is invalid.  Shared by the sync and async /complete
	paths and by /complete/status (re-drive).
	"""
	from werkzeug.utils import secure_filename
	original_filename = secure_filename(meta['filename']) or 'unnamed'
	type_override = meta.get('artefact_type', 'auto')
	type_overridden = False
	if type_override and type_override != 'auto':
		try:
			artefact_type = ArtefactType(type_override)
			type_overridden = True
		except ValueError:
			return None, error_response(f'Invalid artefact_type: {type_override}')
	else:
		artefact_type = detect_artefact_type(meta['filename'])
	return (artefact_type, type_overridden, original_filename), None


def _build_chunk_finalize_fn(meta, artefact_type, type_overridden, original_filename):
	"""Build the ingest closure run off-thread by the async finalise runner.

	Captures only primitives so it is safe to run in the pool thread under a
	fresh app context; re-resolves the Item there and returns the created
	artefact's UUID.  Mirrors the synchronous ingest below.
	"""
	item_uuid = meta['item_uuid']
	label = meta['label']
	description = meta.get('description')
	hints = meta.get('hints') or None
	auto_analyse = meta.get('auto_analyse', True)
	if isinstance(auto_analyse, str):
		auto_analyse = auto_analyse.lower() != 'false'

	def _finalize(assembled):
		item = Item.query.filter_by(uuid=item_uuid).first()
		if item is None:
			raise RuntimeError(f'Item {item_uuid} no longer exists')
		outcome = ingest_uploaded_artefact(
			item,
			label=label,
			artefact_type=artefact_type,
			type_overridden=type_overridden,
			original_filename=original_filename,
			storage_name=assembled.storage_name,
			file_size=assembled.file_size,
			md5=assembled.md5,
			sha256=assembled.sha256,
			description=description,
			hints=hints,
			queue=QUEUE_FULL if auto_analyse else QUEUE_NONE,
		)
		return outcome.artefact.uuid

	return _finalize


@blueprint.route('/uploads/chunked/<string:upload_uuid>/complete', methods=['POST'])
@require_auth('read_upload')
def chunked_upload_complete(upload_uuid):
	"""
	Assemble all chunks and create the artefact.

	Verifies all chunks are present, then either:
	  - synchronously assembles + ingests and returns the artefact (201), or
	  - when the request body sets ``"async": true``, claims the session, runs
	    finalise on the background pool, and returns 202 with a status_url the
	    client polls via /complete/status.  This keeps a large (multi-GB)
	    assembly off the request thread, so it cannot blow the worker timeout.

	Also purges abandoned chunk directories older than 24 h.
	"""
	meta, error = _load_chunk_meta(upload_uuid)
	if error:
		return error

	# Defence in depth: reject an over-size total_chunks before materialising the
	# per-chunk path list, so a tampered/old meta cannot exhaust memory here.
	total_chunks = meta['total_chunks']
	if not isinstance(total_chunks, int) or total_chunks < 1 or total_chunks > _chunked.MAX_TOTAL_CHUNKS:
		return error_response('Upload session corrupt', 400)
	missing = _chunked.missing_chunks(upload_uuid, total_chunks)
	if missing:
		return error_response(f'Missing chunks: {missing}', 400)

	item = _get_item_or_404(meta['item_uuid'])
	manage_error = _require_manage_item_content(item)
	if manage_error:
		return manage_error

	resolved, error = _resolve_chunk_artefact_type(meta)
	if error:
		return error
	artefact_type, type_overridden, original_filename = resolved

	data = request.get_json(silent=True) or {}
	if data.get('async'):
		# Off-thread finalise: claim the session (idempotent — a duplicate
		# /complete just reports the existing state) and submit to the pool.
		finalize_fn = _build_chunk_finalize_fn(
			meta, artefact_type, type_overridden, original_filename)
		if _chunked.claim_finalize(upload_uuid):
			_chunked.submit_finalize(upload_uuid, finalize_fn)
		status = _chunked.finalize_status(upload_uuid) or {'state': 'pending'}
		return jsonify({
			'upload_uuid': upload_uuid,
			'state': status['state'],
			'status_url': request.path + '/status',
		}), 202

	# Synchronous path (unchanged): assemble + ingest inline, return the artefact.
	try:
		assembled = _chunked.assemble_to_storage(
			upload_uuid, original_filename,
			total_chunks=total_chunks, max_size=_chunked.max_upload_size())
	except _chunked.UploadTooLarge as exc:
		return error_response(str(exc), 413)
	except _chunked.StorageUnavailable as exc:
		return error_response(f'Storage backend unavailable: {exc}', 503)

	auto_analyse = meta.get('auto_analyse', True)
	if isinstance(auto_analyse, str):
		auto_analyse = auto_analyse.lower() != 'false'

	# Duplicate check, artefact + slug + analysis queue (single transaction),
	# and orphan-file cleanup on failure all happen in the shared pipeline
	# (identical behaviour to upload_artefact).
	outcome = ingest_uploaded_artefact(
		item,
		label=meta['label'],
		artefact_type=artefact_type,
		type_overridden=type_overridden,
		original_filename=original_filename,
		storage_name=assembled.storage_name,
		file_size=assembled.file_size,
		md5=assembled.md5,
		sha256=assembled.sha256,
		description=meta.get('description'),
		hints=meta.get('hints') or None,
		queue=QUEUE_FULL if auto_analyse else QUEUE_NONE,
	)
	result = artefact_to_dict(outcome.artefact)
	result['queued_analyses'] = [
		t.value for t in outcome.queued_analyses
		if t != AnalysisType.CHECKSUM_COMPUTE
	]
	return jsonify(result), 201


@blueprint.route('/uploads/chunked/<string:upload_uuid>/complete/status', methods=['GET'])
@require_auth('read_upload')
def chunked_upload_complete_status(upload_uuid):
	"""
	Poll the result of an asynchronous finalise.

	Returns one of:
	  - 202 {state: "pending"|"assembling"}  — still working (poll again)
	  - 200 {state: "done", artefact: {...}} — artefact created
	  - 200 {state: "failed", error, error_code}

	If the session is 'assembling' but its heartbeat has gone stale (the runner
	was orphaned by a restart/redeploy), finalise is re-driven here so an upload
	never gets stuck — the client keeps polling and picks up the result.
	"""
	meta, error = _load_chunk_meta(upload_uuid)
	if error:
		return error
	status = _chunked.finalize_status(upload_uuid)
	if status is None:
		return error_response('Upload session not found', 404)

	state = status['state']
	if state == _chunked.FINALIZE_DONE:
		art = Artefact.query.filter_by(uuid=status.get('artefact_uuid')).first()
		if art is None:
			return error_response('Finalised artefact not found', 404)
		return jsonify({'state': 'done', 'artefact': artefact_to_dict(art)}), 200
	if state == _chunked.FINALIZE_FAILED:
		return jsonify({
			'state': 'failed',
			'error': status.get('error'),
			'error_code': status.get('error_code'),
		}), 200

	# pending or assembling: re-drive a stale (orphaned) assembly.
	if state == _chunked.FINALIZE_ASSEMBLING and _chunked.finalize_is_stale(upload_uuid):
		resolved, type_error = _resolve_chunk_artefact_type(meta)
		if not type_error and _chunked.claim_finalize(upload_uuid):
			artefact_type, type_overridden, original_filename = resolved
			_chunked.submit_finalize(
				upload_uuid,
				_build_chunk_finalize_fn(
					meta, artefact_type, type_overridden, original_filename))
	return jsonify({'upload_uuid': upload_uuid, 'state': state}), 202


# =============================================================================
# Hash Database API (for CLI import/export and worker recognition)
# =============================================================================

@blueprint.route('/artefact/<string:uuid>/hash-rescan', methods=['POST'])
@require_auth('read_write')
def run_hash_rescan(uuid):
    """Worker endpoint: run a hash rescan for one artefact and return results.

    Called by the worker when it processes a HASH_RESCAN analysis job.
    Runs rescan_hashes_for_artefact(), optionally queues product recognition,
    and returns {updated, total, recognition_queued}.
    """
    from ..services.hash_rescan import (
        queue_product_recognition_for_partitions,
        rescan_hashes_for_artefact,
    )
    artefact = _get_artefact_or_404(uuid)
    updated, total = rescan_hashes_for_artefact(artefact)

    recognition_queued = 0
    has_recognition = HashDatabase.query.filter_by(
        is_active=True, enable_product_recognition=True
    ).first()
    if has_recognition:
        partition_ids = [p.id for p in artefact.partitions if p.total_files > 0]
        if partition_ids:
            recognition_queued = queue_product_recognition_for_partitions(partition_ids)

    return jsonify({'updated': updated, 'total': total, 'recognition_queued': recognition_queued})


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
    _get_hash_database_or_404(db_id)
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
    _get_known_product_or_404(db_id, pid)
    data = _json_data(force=True)
    if data is None:
        data = {}
    if not isinstance(data, (dict, list)):
        return error_response('JSON object or array required')
    files = data if isinstance(data, list) else data.get('files', [])
    if not files:
        return error_response('files array is required')
    new_kf_list = []
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
        new_kf_list.append(kf)
    database.file_count = (database.file_count or 0) + len(new_kf_list)
    db.session.commit()

    # Link existing extracted files to the new KnownFiles (and queue product
    # recognition when enabled) so an imported database matches the collection
    # immediately, matching the web import route's behaviour.
    link_new_known_files(database, new_kf_list)

    return jsonify({'added': len(new_kf_list)}), 201


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
    # Worker-only: this callback overwrites a partition's product-recognition
    # results (it deletes the existing rows and inserts the supplied ones).  It
    # is gated only on view-visibility of the partition, not on content-
    # management permission, so without this check any read_write user could
    # wipe or falsify recognition results on artefacts they do not own.
    # Matches the worker-only gate on add_partition / add_files.
    if not _is_worker_request():
        return error_response('Only the worker may report recognised products', 403)

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
        product = db.session.get(KnownProduct, product_id)
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


# =============================================================================
# Item sharing ACL endpoints
# =============================================================================

@blueprint.route('/items/<string:uuid>/shares', methods=['GET'])
@require_auth('read_only')
def list_shares(uuid):
    """List all shares for an item.  Restricted to share managers (owner/admin)."""
    if _is_worker_request():
        return error_response('Workers may not access share lists', 403)
    item = _get_item_or_404(uuid)
    user, _ = _api_viewer()
    if not can_manage_shares(item, user):
        return error_response('Not authorised to view shares for this item', 403)
    return jsonify([share_to_dict(s) for s in item.shares])


@blueprint.route('/items/<string:uuid>/shares', methods=['POST'])
@require_auth('read_write')
def add_share(uuid):
    """Add a share to an item.  Workers are not allowed to manage shares."""
    if _is_worker_request():
        return error_response('Workers may not manage shares', 403)
    item = _get_item_or_404(uuid)
    user, _ = _api_viewer()
    if not can_manage_shares(item, user):
        return error_response('Not authorised to manage shares for this item', 403)
    if not item.private_effective:
        return error_response('Only private items can be shared', 400)

    data, err = _json_object(required=True)
    if err:
        return err
    permission = data.get('permission', 'viewer')
    if permission not in SHARE_PERMISSIONS:
        return error_response('permission must be "viewer", "editor", or "curator"', 400)
    if permission == 'curator' and not can_change_owner(item, user):
        return error_response('Only the item owner or an administrator may grant curator access', 403)

    # Resolve user by id or username
    share_user = None
    share_group = None

    if 'user_id' in data:
        try:
            uid = int(data['user_id'])
        except (TypeError, ValueError):
            return error_response('user_id must be an integer', 400)
        share_user = db.session.get(User, uid)
        if not share_user:
            return error_response('User not found', 404)
        if share_user.id == item.owner_id:
            return error_response('The item owner already has full access', 400)
    elif 'username' in data:
        share_user = User.query.filter_by(username=data['username']).first()
        if not share_user:
            return error_response('User not found', 404)
        if share_user.id == item.owner_id:
            return error_response('The item owner already has full access', 400)
    elif 'group_id' in data:
        try:
            gid = int(data['group_id'])
        except (TypeError, ValueError):
            return error_response('group_id must be an integer', 400)
        share_group = db.session.get(Group, gid)
        if not share_group:
            return error_response('Group not found', 404)
        if share_group.name.lower().startswith('arcology-'):
            return error_response('Groups with the "arcology-" prefix are reserved and cannot be used for sharing', 400)
    elif 'group_name' in data:
        share_group = Group.query.filter_by(name=data['group_name'].strip().lower()).first()
        if not share_group:
            return error_response('Group not found', 404)
        if share_group.name.lower().startswith('arcology-'):
            return error_response('Groups with the "arcology-" prefix are reserved and cannot be used for sharing', 400)
    else:
        return error_response('Provide user_id, username, group_id, or group_name')

    if share_user is not None:
        share = ItemShare(item_id=item.id, user_id=share_user.id, permission=permission)
    else:
        share = ItemShare(item_id=item.id, group_id=share_group.id, permission=permission)

    db.session.add(share)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return error_response('Share already exists', 409)
    return jsonify(share_to_dict(share)), 201


@blueprint.route('/items/<string:uuid>/shares/<int:share_id>', methods=['DELETE'])
@require_auth('read_write')
def delete_share(uuid, share_id):
    """Remove a share from an item.  Workers are not allowed to manage shares."""
    if _is_worker_request():
        return error_response('Workers may not manage shares', 403)
    item = _get_item_or_404(uuid)
    user, _ = _api_viewer()
    if not can_manage_shares(item, user):
        return error_response('Not authorised to manage shares for this item', 403)
    share = ItemShare.query.filter_by(id=share_id, item_id=item.id).first()
    if not share:
        return error_response('Share not found', 404)
    db.session.delete(share)
    db.session.commit()
    return jsonify({'status': 'ok'})


# vim: ts=4 sw=4 et
