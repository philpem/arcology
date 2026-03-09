"""
Arcology - API Blueprint

RESTful API for external integrations.
"""

import hmac
import os
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, jsonify, request, current_app, send_file
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import update
from sqlalchemy.orm import joinedload

from ..extensions import db, csrf
from ..database import (
    Item, Artefact, ArtefactType, Analysis, AnalysisType, AnalysisStatus,
    Partition, ExtractedFile, FilesystemType, Platform, Category, Tag,
    ExternalSystem, ExternalReference, HashDatabase, KnownFile, StorageDirectory,
    ApiKey, ApiKeyPermission, _API_KEY_PERMISSION_ORDER,
    ArtefactProtection, ArtefactMastering,
)
from .artefacts import get_artefact_path, _delete_artefact_files, _delete_item_files

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
            key.last_used_at = datetime.now(timezone.utc)
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
    
    if request.args.get('platform_id', type=int):
        query = query.filter(Item.platform_id == request.args.get('platform_id', type=int))
    if request.args.get('category_id', type=int):
        query = query.filter(Item.category_id == request.args.get('category_id', type=int))
    
    pagination = query.order_by(Item.name).paginate(page=page, per_page=per_page)
    return jsonify({
        'items': [item_to_dict(item) for item in pagination.items],
        'total': pagination.total, 'page': page, 'per_page': per_page, 'pages': pagination.pages
    })


@blueprint.route('/items', methods=['POST'])
@require_auth('read_upload')
def create_item():
    data = request.get_json()
    if not data or 'name' not in data:
        return error_response('Name is required')
    
    item = Item(name=data['name'], description=data.get('description'),
                platform_id=data.get('platform_id'), category_id=data.get('category_id'))
    
    if 'tags' in data:
        for tag_name in data['tags']:
            tag = Tag.query.filter_by(name=tag_name).first()
            if not tag:
                tag = Tag(name=tag_name)
                db.session.add(tag)
            item.tags.append(tag)
    
    db.session.add(item)
    db.session.commit()
    return jsonify(item_to_dict(item)), 201


@blueprint.route('/items/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_item(uuid):
    item = Item.query.filter_by(uuid=uuid).first_or_404()
    return jsonify(item_to_dict(item, include_artefacts=True))


@blueprint.route('/items/<string:uuid>', methods=['PUT'])
@require_auth('read_write')
def update_item(uuid):
    item = Item.query.filter_by(uuid=uuid).first_or_404()
    data = request.get_json()
    if 'name' in data: item.name = data['name']
    if 'description' in data: item.description = data['description']
    if 'platform_id' in data: item.platform_id = data['platform_id']
    if 'category_id' in data: item.category_id = data['category_id']
    db.session.commit()
    return jsonify(item_to_dict(item))


@blueprint.route('/items/<string:uuid>', methods=['DELETE'])
@require_auth('read_write')
def delete_item(uuid):
    item = Item.query.filter_by(uuid=uuid).first_or_404()
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
    item = Item.query.filter_by(uuid=item_uuid).first_or_404()
    data = request.get_json()
    if not data or 'label' not in data or 'storage_path' not in data or 'original_filename' not in data:
        return error_response('Label, storage_path and original_filename are required')

    try:
        artefact_type = ArtefactType(data.get('artefact_type', 'other'))
    except ValueError:
        return error_response('Invalid artefact_type')

    try:
        storage_directory = StorageDirectory(data.get('storage_directory', 'uploads'))
    except ValueError:
        return error_response('Invalid storage_directory')

    artefact = Artefact(item_id=item.id, label=data['label'], artefact_type=artefact_type,
                        description=data.get('description'), original_filename=data['original_filename'],
                        storage_path=data['storage_path'], storage_directory=storage_directory,
                        file_size=data.get('file_size'), md5=data.get('md5'), sha256=data.get('sha256'))
    db.session.add(artefact)
    db.session.commit()
    return jsonify(artefact_to_dict(artefact)), 201


@blueprint.route('/artefacts/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_artefact(uuid):
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    return jsonify(artefact_to_dict(artefact, include_partitions=True))


@blueprint.route('/artefacts/<string:uuid>', methods=['DELETE'])
@require_auth('read_write')
def delete_artefact(uuid):
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    _delete_artefact_files(artefact)
    db.session.delete(artefact)
    db.session.commit()
    return '', 204


@blueprint.route('/artefacts/<string:uuid>/download', methods=['GET'])
@require_auth('read_only')
def download_artefact(uuid):
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    full_path = get_artefact_path(artefact)
    if not os.path.exists(full_path):
        return error_response('File not found', 404)
    return send_file(full_path, as_attachment=True, download_name=artefact.original_filename)


# =============================================================================
# Analysis
# =============================================================================

@blueprint.route('/artefacts/<string:uuid>/analysis', methods=['POST'])
@require_auth('read_upload')
def request_analysis(uuid):
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    data = request.get_json() or {}
    try:
        analysis_type = AnalysisType(data.get('analysis_type', 'metadata_extract'))
    except ValueError:
        return error_response('Invalid analysis_type')

    bad_field = _check_nul_bytes(data, ['tool_name', 'hints'])
    if bad_field:
        return error_response(
            f"Field '{bad_field}' contains NUL characters (0x00) which are not permitted in text fields"
        )

    analysis = Analysis(
        artefact_id=artefact.id,
        analysis_type=analysis_type,
        status=AnalysisStatus.PENDING,
        tool_name=data.get('tool_name'),
        hints=data.get('hints')  # Store hints JSON
    )
    db.session.add(analysis)
    db.session.commit()
    return jsonify(analysis_to_dict(analysis)), 201


@blueprint.route('/artefacts/<string:uuid>/analysis', methods=['GET'])
@require_auth('read_only')
def get_artefact_analyses(uuid):
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    return jsonify({'analyses': [analysis_to_dict(a) for a in artefact.analyses]})


@blueprint.route('/analysis/<string:uuid>', methods=['GET'])
@require_auth('read_only')
def get_analysis(uuid):
    analysis = Analysis.query.filter_by(uuid=uuid).first_or_404()
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
    data = request.get_json()

    bad_field = _check_nul_bytes(data, [
        'tool_name', 'tool_version', 'output_url', 'output_path',
        'summary', 'details', 'error_message',
    ])
    if bad_field:
        return error_response(
            f"Field '{bad_field}' contains NUL characters (0x00) which are not permitted in text fields"
        )

    # Handle atomic claim attempt using database-level atomicity
    if data.get('claim_worker') and data.get('status') == 'running':
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
        analysis = Analysis.query.get_or_404(id)
        response = analysis_to_dict(analysis)

        # Add 'claimed' field to indicate if THIS request actually claimed it
        response['claimed'] = result.rowcount > 0
        return jsonify(response)

    # Non-claim updates - use standard ORM approach
    analysis = Analysis.query.get_or_404(id)

    if 'status' in data:
        try:
            analysis.status = AnalysisStatus(data['status'])
        except ValueError:
            return error_response('Invalid status')

    for field in ['tool_name', 'tool_version', 'output_url', 'output_path', 'success', 'summary', 'details', 'error_message']:
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

    db.session.commit()
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
                db.session.add(ArtefactMastering(
                    artefact_id=analysis.artefact_id,
                    mastering_type=ind.get('type', 'unknown'),
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

    except Exception:
        current_app.logger.exception(
            f"Error populating search index for analysis {analysis.uuid} "
            f"({analysis.analysis_type.value})"
        )


@blueprint.route('/analysis/pending', methods=['GET'])
@require_auth('read_only')
def get_pending_analyses():
    """Get pending analyses (for worker)."""
    analyses = Analysis.query.filter(Analysis.status == AnalysisStatus.PENDING).order_by(Analysis.created_at).all()
    return jsonify({'analyses': [analysis_to_dict(a, include_artefact=True) for a in analyses]})


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
    analysis = Analysis.query.get_or_404(id)
    data = request.get_json()
    
    if not data:
        return error_response('JSON body required')
    
    required = ['label', 'original_filename', 'storage_path', 'artefact_type']
    for field in required:
        if field not in data:
            return error_response(f'{field} is required')

    bad_field = _check_nul_bytes(data, ['label', 'original_filename', 'description'])
    if bad_field:
        return error_response(
            f"Field '{bad_field}' contains NUL characters (0x00) which are not permitted in text fields"
        )

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
        prior_derived = (
            Artefact.query
            .join(Analysis, Artefact.derived_from_analysis_id == Analysis.id)
            .filter(
                Artefact.parent_artefact_id == analysis.artefact_id,
                Analysis.analysis_type == analysis.analysis_type,
                Artefact.derived_from_analysis_id != analysis.id,
            )
            .all()
        )
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
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    data = request.get_json()
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
    partition = Partition.query.filter_by(uuid=uuid).first_or_404()
    return jsonify(partition_to_dict(partition))


@blueprint.route('/partitions/<string:uuid>/files', methods=['POST'])
@require_auth('read_upload')
def add_files(uuid):
    partition = Partition.query.filter_by(uuid=uuid).first_or_404()
    data = request.get_json()
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

    response = {'added': added}
    if skipped > 0:
        response['skipped'] = skipped
        current_app.logger.info(f"Skipped {skipped} duplicate files for partition {uuid}")
    return jsonify(response)


@blueprint.route('/partitions/<string:uuid>/files', methods=['GET'])
@require_auth('read_only')
def get_partition_files(uuid):
    partition = Partition.query.filter_by(uuid=uuid).first_or_404()
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
    file = ExtractedFile.query.get_or_404(file_id)
    data = request.get_json()

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
# Helpers
# =============================================================================

def item_to_dict(item, include_artefacts=False):
    result = {
        'id': item.id, 'uuid': item.uuid, 'name': item.name, 'description': item.description,
        'platform': {'id': item.platform.id, 'name': item.platform.name} if item.platform else None,
        'category': {'id': item.category.id, 'name': item.category.name} if item.category else None,
        'tags': [t.name for t in item.tags], 'artefact_count': len(item.artefacts),
        'created_at': item.created_at.isoformat(), 'updated_at': item.updated_at.isoformat()
    }
    if include_artefacts:
        result['artefacts'] = [artefact_to_dict(a) for a in item.artefacts]
    return result


def artefact_to_dict(artefact, include_partitions=False):
    result = {
        'id': artefact.id, 'uuid': artefact.uuid, 'item_id': artefact.item_id,
        'item_uuid': artefact.item.uuid, 'label': artefact.label,
        'artefact_type': artefact.artefact_type.value,
        'type_overridden': artefact.type_overridden,
        'original_filename': artefact.original_filename,
        'file_size': artefact.file_size, 'mime_type': artefact.mime_type,
        'md5': artefact.md5, 'sha256': artefact.sha256,
        'created_at': artefact.created_at.isoformat(), 'updated_at': artefact.updated_at.isoformat()
    }
    if include_partitions:
        result['partitions'] = [partition_to_dict(p) for p in artefact.partitions]
    return result


def analysis_to_dict(analysis, include_artefact=False):
    result = {
        'id': analysis.id, 'uuid': analysis.uuid, 'artefact_id': analysis.artefact_id,
        'artefact_uuid': analysis.artefact.uuid if analysis.artefact else None,
        'analysis_type': analysis.analysis_type.value, 'status': analysis.status.value,
        'tool_name': analysis.tool_name, 'hints': analysis.hints,
        'output_url': analysis.output_url,
        'output_path': analysis.output_path,
        'success': analysis.success, 'summary': analysis.summary, 'error_message': analysis.error_message,
        'details': analysis.details,
        'created_at': analysis.created_at.isoformat(),
        'started_at': analysis.started_at.isoformat() if analysis.started_at else None,
        'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None
    }
    if include_artefact:
        result['artefact'] = {'id': analysis.artefact.id, 'uuid': analysis.artefact.uuid,
                             'label': analysis.artefact.label,
                             'original_filename': analysis.artefact.original_filename,
                             'storage_path': analysis.artefact.storage_path,
                             'storage_directory': analysis.artefact.storage_directory.value,
                             'artefact_type': analysis.artefact.artefact_type.value,
                             'item': {'uuid': analysis.artefact.item.uuid,
                                      'slug': analysis.artefact.item.slug}} if analysis.artefact else None
    return result


def partition_to_dict(partition):
    return {'id': partition.id, 'uuid': partition.uuid, 'partition_index': partition.partition_index,
            'label': partition.label, 'filesystem': partition.filesystem.value,
            'container_format': partition.container_format,
            'total_files': partition.total_files, 'unique_files': partition.unique_files}


def file_to_dict(f):
    return {
        'id': f.id,
        'uuid': f.uuid,
        'path': f.path,
        'filename': f.filename,
        'extension': f.extension,
        'file_size': f.file_size,
        'md5': f.md5,
        'sha1': f.sha1,
        'is_known': f.is_known,
        # Archive support fields
        'risc_os_filetype': f.risc_os_filetype,
        'is_archive': f.is_archive,
        'archive_format': f.archive_format,
        'parent_file_id': f.parent_file_id,
        'extraction_depth': f.extraction_depth
    }


def known_file_to_dict(kf):
    if not kf: return None
    return {'id': kf.id, 'database': kf.database.name, 'filename': kf.filename,
            'product_name': kf.product_name, 'product_version': kf.product_version}


def find_known_file(md5=None, sha1=None, file_size=None):
    query = KnownFile.query
    if md5:
        query = query.filter(KnownFile.md5 == md5.lower())
    elif sha1:
        query = query.filter(KnownFile.sha1 == sha1.lower())
    else:
        return None
    if file_size:
        query = query.filter(KnownFile.file_size == file_size)
    return query.first()


# vim: ts=4 sw=4 et
