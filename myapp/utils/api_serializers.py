"""Shared API serializer helpers for Arcology models."""

from ..services.artefact_lifecycle import build_processing_tree
from .enum_display import enum_value as _enum_value


def share_to_dict(share):
    """Serialise an ItemShare to a JSON-safe dict."""
    return {
        'id': share.id,
        'item_id': share.item_id,
        'user': share.user.username if share.user else None,
        'group': share.group.name if share.group else None,
        'permission': share.permission,
        'created_at': share.created_at.isoformat(),
    }


def item_to_dict(item, include_artefacts=False, _artefact_count=None, _artefacts=None):
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
        'is_private': item.is_private,
        'private_effective': item.private_effective,
        'owner': item.owner.username if item.owner else None,
        'created_at': item.created_at.isoformat(), 'updated_at': item.updated_at.isoformat()
    }
    if include_artefacts:
        artefacts = item.artefacts if _artefacts is None else _artefacts
        result['artefacts'] = [artefact_to_dict(a) for a in artefacts]
    return result


def artefact_to_dict(artefact, include_partitions=False, include_storage=False):
    result = {
        'id': artefact.id, 'uuid': artefact.uuid, 'slug': artefact.slug,
        'item_id': artefact.item_id,
        'item_uuid': artefact.item.uuid, 'item_name': artefact.item.name,
        'item_slug': artefact.item.slug,
        'label': artefact.label,
        'artefact_type': _enum_value(artefact.artefact_type),
        'type_overridden': artefact.type_overridden,
        'original_filename': artefact.original_filename,
        'file_size': artefact.file_size, 'mime_type': artefact.mime_type,
        'md5': artefact.md5, 'sha256': artefact.sha256,
        'tags': [t.name for t in artefact.tags],
        'restrictions': [r.restriction_type.value for r in artefact.restrictions],
        'is_restricted': artefact.is_restricted,
        'is_private': artefact.is_private,
        'effective_private': artefact.effective_private,
        'owner': artefact.owner.username if artefact.owner else None,
        'created_at': artefact.created_at.isoformat(), 'updated_at': artefact.updated_at.isoformat()
    }
    if include_storage:
        blob = artefact.upload_blob or artefact.output_blob
        result['storage_path'] = (
            blob.storage_path if blob is not None else artefact.storage_path
        )
        result['storage_directory'] = artefact.storage_directory.value
    if include_partitions:
        result['partitions'] = [partition_to_dict(p) for p in artefact.partitions]
    return result


def analysis_to_dict(analysis, include_artefact=False, include_artefact_storage=False):
    result = {
        'id': analysis.id, 'uuid': analysis.uuid, 'artefact_id': analysis.artefact_id,
        'artefact_uuid': analysis.artefact.uuid if analysis.artefact else None,
        'analysis_type': _enum_value(analysis.analysis_type), 'status': analysis.status.value,
        'tool_name': analysis.tool_name, 'tool_version': analysis.tool_version, 'hints': analysis.hints,
        'output_url': analysis.output_url,
        'output_path': analysis.output_path,
        'success': analysis.success, 'summary': analysis.summary, 'error_message': analysis.error_message,
        'details': analysis.details,
        'priority': analysis.priority,
        'created_at': analysis.created_at.isoformat(),
        'started_at': analysis.started_at.isoformat() if analysis.started_at else None,
        'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None
    }
    if include_artefact:
        if analysis.artefact:
            art_dict = artefact_to_dict(
                analysis.artefact, include_storage=include_artefact_storage
            )
            # Preserve the nested item shape that worker code reads as
            # artefact['item']['uuid'] / ['slug'] (see arcworker/analyses/*.py).
            art_dict['item'] = {
                'uuid': analysis.artefact.item.uuid,
                'slug': analysis.artefact.item.slug,
            }
            result['artefact'] = art_dict
        else:
            result['artefact'] = None
    return result


def partition_to_dict(partition):
    return {'id': partition.id, 'uuid': partition.uuid, 'partition_index': partition.partition_index,
            'label': partition.label, 'filesystem': partition.filesystem.value,
            'container_format': partition.container_format,
            'archive_comment': partition.archive_comment,
            'total_files': partition.total_files, 'unique_files': partition.unique_files}


def file_to_dict(f):
    return {
        'id': f.id,
        'uuid': f.uuid,
        'path': f.path,
        'filename': f.filename,
        'extension': f.extension,
        'file_size': f.file_size,
        'modified_time': f.modified_time.isoformat() if f.modified_time else None,
        'created_time': f.created_time.isoformat() if f.created_time else None,
        'md5': f.md5,
        'sha1': f.sha1,
        'is_known': f.is_known,
        'is_directory': f.is_directory,
        'risc_os_filetype': f.risc_os_filetype,
        'load_address': f.load_address,
        'exec_address': f.exec_address,
        'attributes': f.attributes,
        'is_archive': f.is_archive,
        'archive_format': f.archive_format,
        'archive_comment': f.archive_comment,
        'parent_file_id': f.parent_file_id,
        'extraction_depth': f.extraction_depth,
    }


def analysis_tree_node(artefact):
    """Build a recursive derivation-tree dict for an artefact.

    Each artefact node contains its analyses, and each analysis contains
    its produced_artefacts (recursively).  Uses analysis_to_dict for the
    analysis nodes so field definitions aren't duplicated.
    """
    from ..database import Analysis, Artefact
    node = {
        'uuid': artefact.uuid, 'slug': artefact.slug, 'label': artefact.label,
        'artefact_type': _enum_value(artefact.artefact_type),
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


def processing_tree_to_dict(root_artefact):
    """Return the full processing tree as a JSON-safe dict.

    Delegates to _build_processing_tree (efficient flat queries, no N+1) and
    converts the resulting ORM-object tree into plain dicts for JSON
    serialisation.

    Return structure::

        {
          'artefact': {  # recursive
            'uuid': ..., 'label': ..., 'artefact_type': ...,
            'original_filename': ..., 'derived_from_analysis_uuid': ...,
            'analyses': [analysis_to_dict(a), ...],
            'path_tree': {               # None when no path-bearing analyses
              'analyses': [...],
              'children': {name: <same>, ...}
            },
            'children': [<same>, ...]    # derived artefact nodes
          },
          'status_counts': {'completed': n, 'failed': n, ...},
          'total_count': n
        }
    """
    tree_node, _has_active, status_counts, total_count = build_processing_tree(root_artefact)

    def _path_tree_to_dict(node):
        return {
            'analyses': [analysis_to_dict(a) for a in node['analyses']],
            'children': {
                name: _path_tree_to_dict(child)
                for name, child in node['children'].items()
            },
        }

    def _node_to_dict(node):
        art = node['artefact']
        return {
            'uuid': art.uuid,
            'slug': art.slug,
            'label': art.label,
            'artefact_type': _enum_value(art.artefact_type),
            'original_filename': art.original_filename,
            'derived_from_analysis_uuid': (
                art.derived_from_analysis.uuid if art.derived_from_analysis_id else None
            ),
            'analyses': [analysis_to_dict(a) for a in node['analyses']],
            'path_tree': (
                _path_tree_to_dict(node['path_tree']) if node['path_tree'] else None
            ),
            'children': [_node_to_dict(c) for c in node['children']],
        }

    return {
        'artefact': _node_to_dict(tree_node),
        'status_counts': status_counts,
        'total_count': total_count,
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
