"""
Arcology - API Blueprint

RESTful API for external integrations.
"""

import os
from datetime import datetime
from flask import Blueprint, jsonify, request, current_app, send_file
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import update

from ..extensions import db, csrf
from ..database import (
    Item, Artefact, ArtefactType, Analysis, AnalysisType, AnalysisStatus,
    Partition, ExtractedFile, FilesystemType, Platform, Category, Tag,
    ExternalSystem, ExternalReference, HashDatabase, KnownFile, StorageDirectory
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/api')


def init_app(app):
    """Exempt API from CSRF protection."""
    csrf.exempt(blueprint)


def error_response(message, status_code=400):
    return jsonify({'error': message}), status_code


# =============================================================================
# Items
# =============================================================================

@blueprint.route('/items', methods=['GET'])
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


@blueprint.route('/items/<int:id>', methods=['GET'])
def get_item(id):
    item = Item.query.get_or_404(id)
    return jsonify(item_to_dict(item, include_artefacts=True))


@blueprint.route('/items/<int:id>', methods=['PUT'])
def update_item(id):
    item = Item.query.get_or_404(id)
    data = request.get_json()
    if 'name' in data: item.name = data['name']
    if 'description' in data: item.description = data['description']
    if 'platform_id' in data: item.platform_id = data['platform_id']
    if 'category_id' in data: item.category_id = data['category_id']
    db.session.commit()
    return jsonify(item_to_dict(item))


@blueprint.route('/items/<int:id>', methods=['DELETE'])
def delete_item(id):
    item = Item.query.get_or_404(id)
    db.session.delete(item)
    db.session.commit()
    return '', 204


# =============================================================================
# Artefacts
# =============================================================================

@blueprint.route('/items/<int:item_id>/artefacts', methods=['POST'])
def add_artefact(item_id):
    item = Item.query.get_or_404(item_id)
    data = request.get_json()
    if not data or 'label' not in data or 'file_path' not in data:
        return error_response('Label and file_path are required')
    
    try:
        artefact_type = ArtefactType(data.get('artefact_type', 'other'))
    except ValueError:
        return error_response('Invalid artefact_type')
    
    artefact = Artefact(item_id=item.id, label=data['label'], artefact_type=artefact_type,
                        description=data.get('description'), file_path=data['file_path'],
                        file_size=data.get('file_size'), md5=data.get('md5'), sha256=data.get('sha256'))
    db.session.add(artefact)
    db.session.commit()
    return jsonify(artefact_to_dict(artefact)), 201


@blueprint.route('/artefacts/<int:id>', methods=['GET'])
def get_artefact(id):
    artefact = Artefact.query.get_or_404(id)
    return jsonify(artefact_to_dict(artefact, include_partitions=True))


@blueprint.route('/artefacts/<int:id>', methods=['DELETE'])
def delete_artefact(id):
    artefact = Artefact.query.get_or_404(id)
    db.session.delete(artefact)
    db.session.commit()
    return '', 204


@blueprint.route('/artefacts/<int:id>/download', methods=['GET'])
def download_artefact(id):
    artefact = Artefact.query.get_or_404(id)
    full_path = os.path.join(current_app.config.get('NAS_BASE_PATH', ''), artefact.file_path.lstrip('/'))
    if not os.path.exists(full_path):
        return error_response('File not found', 404)
    return send_file(full_path, as_attachment=True, download_name=os.path.basename(artefact.file_path))


# =============================================================================
# Analysis
# =============================================================================

@blueprint.route('/artefacts/<int:id>/analysis', methods=['POST'])
def request_analysis(id):
    artefact = Artefact.query.get_or_404(id)
    data = request.get_json() or {}
    try:
        analysis_type = AnalysisType(data.get('analysis_type', 'metadata_extract'))
    except ValueError:
        return error_response('Invalid analysis_type')
    
    analysis = Analysis(artefact_id=artefact.id, analysis_type=analysis_type,
                        status=AnalysisStatus.PENDING, tool_name=data.get('tool_name'))
    db.session.add(analysis)
    db.session.commit()
    return jsonify(analysis_to_dict(analysis)), 201


@blueprint.route('/artefacts/<int:id>/analysis', methods=['GET'])
def get_artefact_analyses(id):
    artefact = Artefact.query.get_or_404(id)
    return jsonify({'analyses': [analysis_to_dict(a) for a in artefact.analyses]})


@blueprint.route('/analysis/<int:id>', methods=['GET'])
def get_analysis(id):
    analysis = Analysis.query.get_or_404(id)
    return jsonify(analysis_to_dict(analysis))


@blueprint.route('/analysis/<int:id>', methods=['PUT'])
def update_analysis(id):
    """
    Update analysis (used by worker).

    Supports atomic claiming: if claim_worker=True and status='running',
    uses an atomic database UPDATE to ensure only one worker can claim
    a job. This prevents race conditions when multiple workers try to
    claim the same job.
    """
    data = request.get_json()

    # Handle atomic claim attempt using database-level atomicity
    if data.get('claim_worker') and data.get('status') == 'running':
        # Use atomic UPDATE with WHERE clause to prevent race conditions
        # Only one worker can successfully transition from PENDING to RUNNING
        result = db.session.execute(
            update(Analysis)
            .where(Analysis.id == id)
            .where(Analysis.status == AnalysisStatus.PENDING)
            .values(status=AnalysisStatus.RUNNING, started_at=datetime.utcnow())
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
        analysis.started_at = datetime.utcnow()
    if data.get('status') in ('completed', 'failed'):
        analysis.completed_at = datetime.utcnow()

    db.session.commit()
    return jsonify(analysis_to_dict(analysis))


@blueprint.route('/analysis/pending', methods=['GET'])
def get_pending_analyses():
    """Get pending analyses (for worker)."""
    analyses = Analysis.query.filter(Analysis.status == AnalysisStatus.PENDING).order_by(Analysis.created_at).all()
    return jsonify({'analyses': [analysis_to_dict(a, include_artefact=True) for a in analyses]})


# =============================================================================
# Output Files
# =============================================================================

@blueprint.route('/outputs/<path:filename>', methods=['GET'])
def get_output_file(filename):
    """Serve an output file (visualisation, etc.)."""
    output_dir = current_app.config.get('OUTPUT_FOLDER', 'outputs')
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(current_app.instance_path, output_dir)
    
    # Security: ensure filename doesn't escape the output directory
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(output_dir, safe_filename)
    
    if not os.path.exists(file_path):
        return error_response('File not found', 404)
    
    return send_file(file_path)


# =============================================================================
# Partitions & Files
# =============================================================================

@blueprint.route('/analysis/<int:id>/produce-artefact', methods=['POST'])
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
    
    # Queue follow-on analyses for the new artefact
    from .artefacts import queue_analyses_for_artefact, ANALYSIS_MAP
    
    # Pass through any hints from parent analysis
    hints = None
    if analysis.hints:
        import json
        hints = json.loads(analysis.hints)
    
    queue_analyses_for_artefact(artefact, hints)
    
    return jsonify({
        'artefact': artefact_to_dict(artefact),
        'queued_analyses': [t.value for t in ANALYSIS_MAP.get(artefact_type, [])]
    }), 201


@blueprint.route('/artefacts/<int:id>/partitions', methods=['POST'])
def add_partition(id):
    artefact = Artefact.query.get_or_404(id)
    data = request.get_json()
    try:
        filesystem = FilesystemType(data.get('filesystem', 'unknown'))
    except ValueError:
        return error_response('Invalid filesystem')
    
    partition = Partition(artefact_id=artefact.id, partition_index=data.get('partition_index', 0),
                          label=data.get('label'), filesystem=filesystem,
                          total_files=data.get('total_files'), total_bytes=data.get('total_bytes'))
    db.session.add(partition)
    db.session.commit()
    return jsonify(partition_to_dict(partition)), 201


@blueprint.route('/partitions/<int:id>/files', methods=['POST'])
def add_files(id):
    partition = Partition.query.get_or_404(id)
    data = request.get_json()
    if 'files' not in data:
        return error_response('files array required')
    
    added = 0
    for f in data['files']:
        if 'path' not in f or 'filename' not in f:
            continue
        ef = ExtractedFile(partition_id=partition.id, path=f['path'], filename=f['filename'],
                           extension=f.get('extension'), file_size=f.get('file_size'),
                           md5=f.get('md5'), sha1=f.get('sha1'), crc32=f.get('crc32'))
        
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
    return jsonify({'added': added})


@blueprint.route('/partitions/<int:id>/files', methods=['GET'])
def get_partition_files(id):
    partition = Partition.query.get_or_404(id)
    show_known = request.args.get('show_known', 'false').lower() == 'true'
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)
    
    query = ExtractedFile.query.filter_by(partition_id=partition.id)
    if not show_known:
        query = query.filter(ExtractedFile.is_known == False)
    
    pagination = query.order_by(ExtractedFile.path).paginate(page=page, per_page=per_page)
    return jsonify({
        'files': [file_to_dict(f) for f in pagination.items],
        'total': pagination.total, 'page': page, 'per_page': per_page, 'pages': pagination.pages
    })


# =============================================================================
# Lookup
# =============================================================================

@blueprint.route('/lookup', methods=['GET'])
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
    
    extracted = query.all()
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
        'id': item.id, 'name': item.name, 'description': item.description,
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
        'id': artefact.id, 'item_id': artefact.item_id, 'label': artefact.label,
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
        'id': analysis.id, 'artefact_id': analysis.artefact_id,
        'analysis_type': analysis.analysis_type.value, 'status': analysis.status.value,
        'tool_name': analysis.tool_name, 'hints': analysis.hints,
        'output_url': analysis.output_url,
        'success': analysis.success, 'summary': analysis.summary, 'error_message': analysis.error_message,
        'created_at': analysis.created_at.isoformat(),
        'started_at': analysis.started_at.isoformat() if analysis.started_at else None,
        'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None
    }
    if include_artefact:
        result['artefact'] = {'id': analysis.artefact.id, 'label': analysis.artefact.label,
                             'original_filename': analysis.artefact.original_filename,
                             'storage_path': analysis.artefact.storage_path,
                             'storage_directory': analysis.artefact.storage_directory.value,
                             'artefact_type': analysis.artefact.artefact_type.value}
    return result


def partition_to_dict(partition):
    return {'id': partition.id, 'partition_index': partition.partition_index, 'label': partition.label,
            'filesystem': partition.filesystem.value, 'total_files': partition.total_files, 'unique_files': partition.unique_files}


def file_to_dict(f):
    return {'id': f.id, 'path': f.path, 'filename': f.filename, 'extension': f.extension,
            'file_size': f.file_size, 'md5': f.md5, 'sha1': f.sha1, 'is_known': f.is_known}


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


# vim: ts=4 sw=4 noet
