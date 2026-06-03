"""
Arcology - API Blueprint

RESTful API for external integrations.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Blueprint, abort, current_app, g, jsonify, redirect, request, send_file
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import contains_eager, joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError
from ..database import (
    _API_KEY_PERMISSION_ORDER,
    Analysis,
    AnalysisStatus,
    AnalysisType,
    ApiKey,
    ApiKeyPermission,
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
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
    RiscosModule,
    StorageDirectory,
    Tag,
    User,
)
from ..extensions import csrf, db
from ..utils.api_serializers import (
    analysis_to_dict,
    artefact_to_dict,
    file_to_dict,
    item_to_dict,
    known_file_to_dict,
    partition_to_dict,
    share_to_dict,
)
from ..utils.db_helpers import get_by_id_or_404 as _get_by_id_or_404
from ..utils.db_helpers import get_by_uuid_or_404 as _get_by_uuid_or_404
from ..utils.hash_rescan import find_known_file
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
)
from .artefacts import (
    _artefact_contained_file_restrictions,
    _collect_all_file_restrictions,
    _collect_ancestor_file_restrictions,
    _delete_artefact_files,
    _get_storage_extension,
    bulk_delete_item,
    compute_file_hashes,
    detect_artefact_type,
    get_artefact_path,
    get_artefact_storage_key,
    move_artefact_to_item,
    queue_analyses_for_artefact,
    save_uploaded_file,
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
    """Abort with 404 if the API caller may not view an analysis' artefact."""
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
    db.session.commit()
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

    api_user, sees_all = _api_viewer()
    if item.private_effective and not can_contribute_to_item(item, api_user, sees_all=sees_all):
        return error_response('Not permitted to add artefacts to this item', 403)
    artefact = Artefact(item_id=item.id, label=data['label'], artefact_type=artefact_type,
                        description=data.get('description'), original_filename=data['original_filename'],
                        storage_path=data['storage_path'], storage_directory=storage_directory,
                        file_size=data.get('file_size'), md5=data.get('md5'), sha256=data.get('sha256'),
                        owner_id=(api_user.id if api_user is not None else item.owner_id),
                        is_private=bool(data.get('is_private', False)))
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
    api_user, sees_all = _api_viewer()
    if artefact.item.private_effective and not (sees_all or can_change_owner(artefact.item, api_user)):
        return error_response('Not permitted to delete artefacts from this item', 403)
    _delete_artefact_files(artefact)
    db.session.delete(artefact)
    db.session.commit()
    return '', 204


@blueprint.route('/artefacts/<string:uuid>/move', methods=['POST'])
@require_auth('read_write')
def move_artefact(uuid):
    """Move a root artefact (and its derived artefacts) to a different item."""
    artefact = _get_artefact_or_404(uuid)
    api_user, sees_all = _api_viewer()
    if artefact.item.private_effective and not can_contribute_to_item(artefact.item, api_user, sees_all=sees_all):
        return error_response('Not permitted to move artefacts from this item', 403)
    data, error = _json_object()
    if error:
        return error

    target_uuid = data.get('target_item_uuid')
    if not target_uuid:
        return error_response('target_item_uuid is required', 400)

    if artefact.parent_artefact_id is not None:
        return error_response('Only root artefacts can be moved', 400)

    target_item = Item.query.filter_by(uuid=target_uuid).first()
    if not target_item:
        return error_response('Target item not found', 404)
    if not can_view_item(target_item, api_user, sees_all=sees_all):
        return error_response('Target item not found', 404)
    # A curator on the source (not the owner/admin) must not be able to move
    # artefacts into a public item — that would silently publish private content.
    # Require write access on the target whenever the source is private and the
    # caller is acting as a curator rather than as owner/admin.
    curator_on_source = (artefact.item.private_effective
                         and not can_change_owner(artefact.item, api_user))
    if (curator_on_source or target_item.private_effective) and \
            not can_contribute_to_item(target_item, api_user, sees_all=sees_all):
        return error_response('Not permitted to move artefacts into this item', 403)

    if target_item.id == artefact.item_id:
        return error_response('Artefact is already in that item', 400)

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
    if 'md5' in data:
        artefact.md5 = data['md5']
    if 'sha256' in data:
        artefact.sha256 = data['sha256']
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
    return jsonify(artefact_to_dict(artefact))


@blueprint.route('/artefacts/<string:uuid>/download', methods=['GET'])
@require_auth('read_only')
def download_artefact(uuid):
    artefact = _get_artefact_or_404(uuid, selectinload(Artefact.restrictions))

    # Enforce download restrictions, honouring the caller's bypass grants.
    user, _ = _api_viewer()
    if not can_download_despite_restrictions(user, artefact.restrictions, artefact):
        return jsonify({
            'error': 'Download restricted',
            'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        }), 403

    # Block the original download when its extracted contents are restricted
    # (mirrors the website's _check_artefact_file_restrictions).
    contained = _artefact_contained_file_restrictions(artefact)
    if not can_download_despite_restrictions(user, contained, artefact):
        return jsonify({
            'error': 'Download restricted (artefact contains restricted files)',
            'restrictions': list({r.restriction_type.value for r in contained}),
        }), 403

    storage = current_app.storage
    key = get_artefact_storage_key(artefact)

    # S3 mode: redirect to pre-signed URL
    url = storage.presigned_url(key, filename=artefact.original_filename)
    if url:
        return redirect(url)

    # Local mode: serve file directly
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
    _require_view_artefact(artefact)

    # Restriction gate, honouring the caller's bypass grants (same policy as web).
    user, _ = _api_viewer()
    if not can_download_despite_restrictions(user, artefact.restrictions, artefact):
        return jsonify({
            'error': 'Download restricted',
            'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        }), 403

    # Check file-level restrictions: own, descendants (archive contains restricted file),
    # and ancestors (file is inside a restricted archive).
    file_restrictions = (
        _collect_all_file_restrictions(ef) + _collect_ancestor_file_restrictions(ef)
    )
    if not can_download_despite_restrictions(user, file_restrictions, artefact):
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
    from .artefacts import _collect_all_analyses
    artefact = _get_artefact_or_404(uuid)
    analyses = _collect_all_analyses(artefact)

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
        # Serialise concurrent completions of the same analysis type for the
        # same artefact (e.g. two workers completing re-analysis jobs at the
        # same time).  Without this lock, both workers delete each other's
        # freshly-inserted search-index rows in the delete-then-insert pattern
        # below.  The lock is released when the outer transaction commits.
        db.session.execute(
            select(Artefact).where(Artefact.id == analysis.artefact_id).with_for_update()
        )

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
            path_prefix = details.get('path_prefix', '')
            if path_prefix:
                # Scoped deletion: only remove modules from this archive's
                # path prefix so concurrent nested-archive jobs don't clobber
                # each other's results.
                RiscosModule.query.filter(
                    RiscosModule.artefact_id == analysis.artefact_id,
                    RiscosModule.file_path.like(path_prefix + '/%'),
                ).delete(synchronize_session=False)
            elif details.get('modules'):
                # Only clear all when top-level scan actually found modules
                # (disc image case). If 0 modules found with no prefix, preserve
                # rows from nested-archive scans to avoid a race where a
                # re-analysis top-level job runs before nested archives are
                # re-extracted and wipes all previously stored module data.
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
    """Get pending analyses (for worker).

    Optional query parameter:
        types: comma-separated AnalysisType names to restrict results to
               (e.g. ``?types=FLUX_VISUALISATION,FLUX_DECODE``).
               Unknown names are silently ignored.
    """
    query = (
        Analysis.query
        .filter(Analysis.status == AnalysisStatus.PENDING)
        .options(joinedload(Analysis.artefact).joinedload(Artefact.item))
    )
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
    """Serve an output file (visualisation, etc.)."""
    storage = current_app.storage
    key = storage.storage_key('outputs', filename)

    # S3 mode: redirect to pre-signed URL
    url = storage.presigned_url(key)
    if url:
        return redirect(url)

    # Local mode: serve file directly with path traversal protection
    from shared.storage import LocalStorage
    if isinstance(storage, LocalStorage):
        local_path = str(storage.local_path(key))
        output_dir = str(storage.outputs_dir)
        real_path = os.path.realpath(local_path)
        if not real_path.startswith(os.path.realpath(output_dir) + os.sep):
            return error_response('File not found', 404)
        if not os.path.exists(real_path):
            return error_response('File not found', 404)
        mime, _ = mimetypes.guess_type(real_path)
        return send_file(real_path, mimetype=mime or 'application/octet-stream')

    return error_response('File not found', 404)


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
        derived_from_analysis_id=analysis.id,
        # Derived artefacts inherit the source artefact's owner and privacy flag.
        # Item-level privacy descends automatically via Item.private_effective, but
        # artefact-level is_private must be copied explicitly to avoid exposing a
        # private artefact's derived outputs when the parent item is public.
        owner_id=analysis.artefact.owner_id,
        is_private=analysis.artefact.is_private,
    )
    
    db.session.add(artefact)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # SHA-256 collision: another artefact in this item already has this content.
        # This can happen when a directly-uploaded SCP and a DFI-derived SCP both
        # produce the same HFE/RAW_SECTOR output (same bytes, different lineage), or
        # when re-analysis leaves orphaned grandchildren whose cascade delete failed.
        #
        # Strategy: re-home the existing artefact under the current analysis so the
        # UI shows it in the correct place.  We update parent_artefact_id,
        # derived_from_analysis_id, label, and original_filename to match the current
        # request.  The file in storage is kept as-is (same content); the new
        # (orphaned) file the worker uploaded is deleted from storage.
        sha256 = data.get('sha256')
        existing = (
            Artefact.query
            .filter_by(item_id=analysis.artefact.item_id, sha256=sha256)
            .first()
        ) if sha256 else None
        if not existing:
            return error_response('Failed to create derived artefact (integrity error)', 500)

        current_app.logger.info(
            f"produce_artefact: SHA-256 collision — re-homing artefact {existing.uuid} "
            f"(was analysis {existing.derived_from_analysis_id}) under analysis {analysis.id}"
        )

        # Delete the orphaned new file the worker uploaded (if different from existing)
        if data.get('storage_path') and data['storage_path'] != existing.storage_path:
            try:
                orphan_key = current_app.storage.storage_key(
                    storage_directory.value, data['storage_path']
                )
                current_app.storage.delete(orphan_key)
            except Exception as e:
                current_app.logger.warning(
                    f"produce_artefact: failed to delete orphaned file "
                    f"{data['storage_path']}: {e}"
                )

        existing.parent_artefact_id = analysis.artefact_id
        existing.derived_from_analysis_id = analysis.id
        existing.label = data['label']
        existing.original_filename = data['original_filename']
        existing.slug = ensure_unique_slug(
            generate_slug(existing.label), Artefact,
            existing_id=existing.id, scope_filter={'item_id': existing.item_id},
        )
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"produce_artefact: re-home commit failed: {e}")
            return error_response('Failed to re-home existing artefact', 500)

        # Queue follow-on analyses on the re-homed artefact just like a freshly-
        # created derived artefact.  Without this, the re-homed artefact retains
        # only whatever analyses/partitions/files it had under the old lineage,
        # which may be missing (e.g. orphaned after a failed cascade) or stale.
        # queue_analyses_for_artefact skips PENDING/RUNNING duplicates, so this
        # is safe to call even if some analyses are already active.
        queued_analyses = []
        if data.get('auto_analyse', True):
            from .artefacts import ANALYSIS_MAP
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

    # Generate slug (unique within this item)
    artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': analysis.artefact.item_id})
    db.session.commit()

    # Queue follow-on analyses unless the caller will handle that explicitly
    queued_analyses = []
    if data.get('auto_analyse', True):
        from .artefacts import ANALYSIS_MAP
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

        if ef.md5 or ef.sha1:
            known = find_known_file(md5=ef.md5, sha1=ef.sha1, file_size=ef.file_size)
            if known:
                ef.known_file_id = known.id
                ef.is_known = True
        db.session.add(ef)
        added += 1

    partition.total_files = ExtractedFile.query.filter_by(partition_id=partition.id).count()
    partition.unique_files = ExtractedFile.query.filter_by(partition_id=partition.id, is_known=False).count()
    try:
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        # pgcode '40P01' is DeadlockDetected — return 503 so the worker can
        # retry rather than waiting for the stale-job timeout to fire.
        if getattr(exc.orig, 'pgcode', None) == '40P01':
            return jsonify({'error': 'Deadlock detected, please retry'}), 503
        raise

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

	# Check for duplicate: same item + same SHA-256
	existing = Artefact.query.filter_by(item_id=item.id, sha256=sha256).first()
	if existing:
		# Clean up the uploaded file — it's a duplicate
		try:
			current_app.storage.delete(storage_key)
		except Exception:
			pass
		result = artefact_to_dict(existing)
		result['duplicate'] = True
		return jsonify(result), 409

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
	try:
		db.session.commit()
	except IntegrityError:
		# Two concurrent uploads of the same file (same item + sha256) raced
		# past the application-level duplicate check.  The DB constraint fired;
		# roll back, clean up the orphaned file, and return the existing record.
		db.session.rollback()
		try:
			current_app.storage.delete(storage_key)
		except Exception:
			pass
		existing = Artefact.query.filter_by(item_id=item.id, sha256=sha256).first()
		if existing:
			result = artefact_to_dict(existing)
			result['duplicate'] = True
			return jsonify(result), 409
		raise  # unexpected — sha256 is None or constraint is on a different column

	# Generate slug (unique within this item)
	artefact.slug = ensure_unique_slug(generate_slug(artefact.label), Artefact, scope_filter={'item_id': item.id})
	db.session.commit()

	# Parse optional hints (JSON string from form field)
	hints = None
	hints_raw = request.form.get('hints')
	if hints_raw:
		try:
			hints = json.loads(hints_raw)
			if not isinstance(hints, dict):
				return error_response('hints must be a JSON object')
		except json.JSONDecodeError:
			return error_response('hints must be valid JSON')

	# Optionally queue analyses
	queued_analyses = []
	auto_analyse = request.form.get('auto_analyse', 'true').lower() != 'false'
	if auto_analyse:
		from .artefacts import ANALYSIS_MAP
		queue_analyses_for_artefact(artefact, hints)
		queued_analyses = [t.value for t in ANALYSIS_MAP.get(artefact_type, [])]

	result = artefact_to_dict(artefact)
	result['queued_analyses'] = queued_analyses
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

_UPLOAD_UUID_RE = re.compile(r'^[0-9a-f]{32}$')
_CHUNK_STALE_SECONDS = 86400  # purge abandoned chunk dirs after 24 h


def _chunks_base() -> str:
	"""Return (and create) the base directory for in-progress chunk uploads."""
	path = os.path.join(current_app.instance_path, '.chunks')
	os.makedirs(path, exist_ok=True)
	return path


def _chunk_dir(upload_uuid: str) -> str:
	return os.path.join(_chunks_base(), upload_uuid)


def _purge_stale_chunks() -> None:
	"""Remove chunk directories that have not been touched in > 24 h."""
	base = _chunks_base()
	cutoff = datetime.now(timezone.utc).timestamp() - _CHUNK_STALE_SECONDS
	try:
		for name in os.listdir(base):
			if not _UPLOAD_UUID_RE.match(name):
				continue
			path = os.path.join(base, name)
			try:
				if os.stat(path).st_mtime < cutoff:
					shutil.rmtree(path, ignore_errors=True)
			except OSError:
				pass
	except OSError:
		pass


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

	upload_uuid = uuid.uuid4().hex
	chunk_dir = _chunk_dir(upload_uuid)
	os.makedirs(chunk_dir, exist_ok=True)

	hints = data.get('hints')
	if hints is not None and not isinstance(hints, dict):
		return error_response('hints must be a JSON object')

	meta = {
		'filename': filename,
		'total_chunks': total_chunks,
		'total_size': data.get('total_size'),
		'item_uuid': item_uuid,
		'label': label,
		'artefact_type': data.get('artefact_type', 'auto'),
		'description': data.get('description'),
		'auto_analyse': data.get('auto_analyse', True),
		'hints': hints,
		'created_at': datetime.now(timezone.utc).isoformat(),
	}
	with open(os.path.join(chunk_dir, 'meta.json'), 'w') as f:
		json.dump(meta, f)

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
	if not _UPLOAD_UUID_RE.match(upload_uuid):
		return error_response('Upload session not found', 404)

	chunk_dir = _chunk_dir(upload_uuid)
	if not os.path.isdir(chunk_dir):
		return error_response('Upload session not found', 404)

	if chunk_index < 0:
		return error_response('chunk_index must be non-negative')

	chunk_path = os.path.join(chunk_dir, f'{chunk_index:06d}')
	with open(chunk_path, 'wb') as f:
		f.write(request.data)

	return jsonify({'received': True, 'chunk': chunk_index})


@blueprint.route('/uploads/chunked/<string:upload_uuid>/status', methods=['GET'])
@require_auth('read_upload')
def chunked_upload_status(upload_uuid):
	"""
	Query which chunks have been received.

	Returns {"upload_uuid": "...", "total_chunks": N, "received_chunks": [0, 1, ...]}.
	Allows clients to resume after a partial failure.
	"""
	if not _UPLOAD_UUID_RE.match(upload_uuid):
		return error_response('Upload session not found', 404)

	chunk_dir = _chunk_dir(upload_uuid)
	if not os.path.isdir(chunk_dir):
		return error_response('Upload session not found', 404)

	meta_path = os.path.join(chunk_dir, 'meta.json')
	try:
		with open(meta_path) as f:
			meta = json.load(f)
	except (OSError, json.JSONDecodeError):
		return error_response('Upload session corrupt', 500)

	received = sorted(
		int(name) for name in os.listdir(chunk_dir)
		if name != 'meta.json' and name.isdigit()
	)

	return jsonify({
		'upload_uuid': upload_uuid,
		'total_chunks': meta['total_chunks'],
		'received_chunks': received,
	})


@blueprint.route('/uploads/chunked/<string:upload_uuid>/complete', methods=['POST'])
@require_auth('read_upload')
def chunked_upload_complete(upload_uuid):
	"""
	Assemble all chunks and create the artefact.

	Verifies all chunks are present, assembles them into a temp file (computing
	hashes inline), then pushes to the storage backend and creates an Artefact
	record.  Returns the same 201 JSON as the regular upload endpoint.

	Also purges abandoned chunk directories older than 24 h.
	"""
	if not _UPLOAD_UUID_RE.match(upload_uuid):
		return error_response('Upload session not found', 404)

	chunk_dir = _chunk_dir(upload_uuid)
	if not os.path.isdir(chunk_dir):
		return error_response('Upload session not found', 404)

	meta_path = os.path.join(chunk_dir, 'meta.json')
	try:
		with open(meta_path) as f:
			meta = json.load(f)
	except (OSError, json.JSONDecodeError):
		return error_response('Upload session corrupt', 500)

	total_chunks = meta['total_chunks']
	chunk_files = [os.path.join(chunk_dir, f'{i:06d}') for i in range(total_chunks)]
	missing = [i for i, p in enumerate(chunk_files) if not os.path.exists(p)]
	if missing:
		return error_response(f'Missing chunks: {missing}', 400)

	item = _get_item_or_404(meta['item_uuid'])
	manage_error = _require_manage_item_content(item)
	if manage_error:
		return manage_error

	# Determine artefact type
	from werkzeug.utils import secure_filename
	original_filename = secure_filename(meta['filename']) or 'unnamed'
	type_override = meta.get('artefact_type', 'auto')
	type_overridden = False
	if type_override and type_override != 'auto':
		try:
			artefact_type = ArtefactType(type_override)
			type_overridden = True
		except ValueError:
			return error_response(f'Invalid artefact_type: {type_override}')
	else:
		artefact_type = detect_artefact_type(meta['filename'])

	# Generate storage name (same pattern as save_uploaded_file)
	ext = _get_storage_extension(original_filename)
	storage_name = f'{uuid.uuid4().hex}{ext}'

	# Assemble chunks into a temp file, computing hashes inline
	md5_hash = hashlib.md5()
	sha256_hash = hashlib.sha256()
	file_size = 0

	try:
		with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
			tmp_path = tmp.name
			for chunk_file in chunk_files:
				with open(chunk_file, 'rb') as cf:
					while True:
						buf = cf.read(65536)
						if not buf:
							break
						tmp.write(buf)
						md5_hash.update(buf)
						sha256_hash.update(buf)
						file_size += len(buf)

		# Push assembled file to storage backend
		storage_key = current_app.storage.storage_key('uploads', storage_name)
		try:
			current_app.storage.put(storage_key, tmp_path)
		except OSError as exc:
			return error_response(f'Storage backend unavailable: {exc}', 503)
	finally:
		try:
			os.unlink(tmp_path)
		except OSError:
			pass

	# Clean up chunk directory and purge any stale sessions
	shutil.rmtree(chunk_dir, ignore_errors=True)
	_purge_stale_chunks()

	md5 = md5_hash.hexdigest()
	sha256 = sha256_hash.hexdigest()

	# Check for duplicate: same item + same SHA-256
	existing = Artefact.query.filter_by(item_id=item.id, sha256=sha256).first()
	if existing:
		# Clean up the uploaded file — it's a duplicate
		try:
			current_app.storage.delete(storage_key)
		except Exception:
			pass
		result = artefact_to_dict(existing)
		result['duplicate'] = True
		return jsonify(result), 409

	# Create artefact record (identical logic to upload_artefact)
	artefact = Artefact(
		item_id=item.id,
		label=meta['label'],
		artefact_type=artefact_type,
		type_overridden=type_overridden,
		description=meta.get('description'),
		original_filename=original_filename,
		storage_path=storage_name,
		storage_directory=StorageDirectory.UPLOADS,
		file_size=file_size,
		md5=md5,
		sha256=sha256,
	)
	db.session.add(artefact)
	try:
		db.session.commit()
	except IntegrityError:
		# Race: two concurrent chunked uploads of the same file reached the
		# commit simultaneously.  Roll back, delete the orphaned file, and
		# return the record that the other request created.
		db.session.rollback()
		try:
			current_app.storage.delete(storage_key)
		except Exception:
			pass
		existing = Artefact.query.filter_by(item_id=item.id, sha256=sha256).first()
		if existing:
			result = artefact_to_dict(existing)
			result['duplicate'] = True
			return jsonify(result), 409
		raise

	artefact.slug = ensure_unique_slug(
		generate_slug(artefact.label), Artefact, scope_filter={'item_id': item.id}
	)
	db.session.commit()

	queued_analyses = []
	auto_analyse = meta.get('auto_analyse', True)
	if isinstance(auto_analyse, str):
		auto_analyse = auto_analyse.lower() != 'false'
	if auto_analyse:
		from .artefacts import ANALYSIS_MAP
		hints = meta.get('hints') or None
		queue_analyses_for_artefact(artefact, hints)
		queued_analyses = [t.value for t in ANALYSIS_MAP.get(artefact_type, [])]

	result = artefact_to_dict(artefact)
	result['queued_analyses'] = queued_analyses
	return jsonify(result), 201


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
    from ..utils.hash_rescan import (
        queue_product_recognition_for_partitions,
        rescan_hashes_for_artefact,
    )
    artefact = _get_by_uuid_or_404(Artefact, uuid)
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
