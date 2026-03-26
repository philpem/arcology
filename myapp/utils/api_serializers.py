"""Shared API serializer helpers for Arcology models."""


def item_to_dict(item, include_artefacts=False, _artefact_count=None):
    result = {
        'id': item.id, 'uuid': item.uuid, 'name': item.name, 'description': item.description,
        'platform': {'id': item.platform.id, 'name': item.platform.name} if item.platform else None,
        'category': {'id': item.category.id, 'name': item.category.name} if item.category else None,
        'tags': [t.name for t in item.tags],
        'artefact_count': _artefact_count if _artefact_count is not None else len(item.artefacts),
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
        'tags': [t.name for t in artefact.tags],
        'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        'is_restricted': artefact.is_restricted,
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
        'risc_os_filetype': f.risc_os_filetype,
        'is_archive': f.is_archive,
        'archive_format': f.archive_format,
        'parent_file_id': f.parent_file_id,
        'extraction_depth': f.extraction_depth
    }


def known_file_to_dict(kf):
    if not kf:
        return None
    product = kf.product
    return {
        'id': kf.id,
        'database': kf.database.name,
        'filename': kf.filename,
        'product_name': product.title if product else kf.product_name,
        'product_version': kf.product_version,
    }
