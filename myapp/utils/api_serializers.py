"""Shared API serializer helpers for Arcology models."""


def item_to_dict(item, include_artefacts=False, _artefact_count=None):
    ep = item.effective_platform
    ec = item.effective_category
    result = {
        'id': item.id, 'uuid': item.uuid, 'name': item.name, 'slug': item.slug, 'description': item.description,
        'parent_uuid': item.parent.uuid if item.parent else None,
        'parent_name': item.parent.name if item.parent else None,
        'path': [{'uuid': a.uuid, 'name': a.name} for a in item.ancestors],
        'platform': {'id': item.platform.id, 'name': item.platform.name} if item.platform else None,
        'category': {'id': item.category.id, 'name': item.category.name} if item.category else None,
        'effective_platform': {'id': ep.id, 'name': ep.name} if ep else None,
        'effective_category': {'id': ec.id, 'name': ec.name} if ec else None,
        'tags': [t.name for t in item.tags],
        'child_count': len(item.children),
        'artefact_count': _artefact_count if _artefact_count is not None else len(item.artefacts),
        'created_at': item.created_at.isoformat(), 'updated_at': item.updated_at.isoformat()
    }
    if include_artefacts:
        result['artefacts'] = [artefact_to_dict(a) for a in item.artefacts]
    return result


def artefact_to_dict(artefact, include_partitions=False):
    result = {
        'id': artefact.id, 'uuid': artefact.uuid, 'item_id': artefact.item_id,
        'item_uuid': artefact.item.uuid, 'item_name': artefact.item.name,
        'label': artefact.label,
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
        'tool_name': analysis.tool_name, 'tool_version': analysis.tool_version, 'hints': analysis.hints,
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
                             'slug': analysis.artefact.slug,
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
        'is_directory': f.is_directory,
        'risc_os_filetype': f.risc_os_filetype,
        'is_archive': f.is_archive,
        'archive_format': f.archive_format,
        'parent_file_id': f.parent_file_id,
        'extraction_depth': f.extraction_depth,
        'is_directory': f.is_directory,
    }


def analysis_tree_node(artefact):
    """Build a recursive derivation-tree dict for an artefact.

    Each artefact node contains its analyses, and each analysis contains
    its produced_artefacts (recursively).  Uses analysis_to_dict for the
    analysis nodes so field definitions aren't duplicated.
    """
    from ..database import Analysis, Artefact
    node = {
        'uuid': artefact.uuid, 'label': artefact.label,
        'artefact_type': artefact.artefact_type.value,
        'original_filename': artefact.original_filename,
        'parent_artefact_uuid': artefact.parent.uuid if artefact.parent_artefact_id else None,
        'derived_from_analysis_uuid': artefact.derived_from_analysis.uuid if artefact.derived_from_analysis_id else None,
    }
    analyses = Analysis.query.filter_by(artefact_id=artefact.id).order_by(Analysis.id).all()
    node['analyses'] = []
    for an in analyses:
        an_dict = analysis_to_dict(an)
        produced = Artefact.query.filter_by(derived_from_analysis_id=an.id).order_by(Artefact.id).all()
        an_dict['produced_artefacts'] = [analysis_tree_node(p) for p in produced]
        node['analyses'].append(an_dict)
    return node


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
