"""
Hierarchical output path generation for Arcology worker.

Creates structured output directories:
{item_uuid}_{item_slug}/
  {artefact_uuid}_{artefact_slug}/
    {analysis_uuid}_{analysis_slug}/
      partition_{index}_{slug}/
        (extracted files)
"""

from pathlib import Path
from typing import Optional, Dict, Any


def get_output_path(
    output_base: Path,
    item: Dict[str, Any],
    artefact: Dict[str, Any],
    analysis: Dict[str, Any],
    partition: Optional[Dict[str, Any]] = None
) -> Path:
    """
    Generate hierarchical output directory path.

    Creates directory structure: item/artefact/analysis/partition (optional)

    Args:
        output_base: Base output directory (from config.OUTPUT_DIR)
        item: Item dict with 'uuid' and 'slug' keys
        artefact: Artefact dict with 'uuid' and 'slug' keys
        analysis: Analysis dict with 'uuid' and 'slug' keys
        partition: Optional partition dict with 'partition_index' and 'slug' keys

    Returns:
        Path object for output directory (created if doesn't exist)

    Examples:
        >>> get_output_path(
        ...     Path('/data/outputs'),
        ...     {'uuid': 'abc123', 'slug': 'risc-os-3-11'},
        ...     {'uuid': 'def456', 'slug': 'disc-1-install'},
        ...     {'uuid': 'ghi789', 'slug': 'file-listing'},
        ...     {'partition_index': 0, 'slug': 'system'}
        ... )
        Path('/data/outputs/abc123_risc-os-3-11/def456_disc-1-install/
              ghi789_file-listing/partition_0_system')
    """
    # Get slugs with fallback to 'untitled' if not present
    item_slug = item.get('slug') or 'untitled'
    artefact_slug = artefact.get('slug') or 'untitled'
    analysis_slug = analysis.get('slug') or 'untitled'

    # Build path: item → artefact → analysis
    path = output_base / f"{item['uuid']}_{item_slug}"
    path = path / f"{artefact['uuid']}_{artefact_slug}"
    path = path / f"{analysis['uuid']}_{analysis_slug}"

    # Add partition subdirectory if specified
    if partition:
        partition_index = partition.get('partition_index', 0)
        partition_slug = partition.get('slug') or str(partition_index)
        path = path / f"partition_{partition_index}_{partition_slug}"

    # Create directory if it doesn't exist
    path.mkdir(parents=True, exist_ok=True)

    return path


def get_item_path(output_base: Path, item: Dict[str, Any]) -> Path:
    """
    Get item-level directory path.

    Args:
        output_base: Base output directory
        item: Item dict with 'uuid' and 'slug' keys

    Returns:
        Path to item directory
    """
    item_slug = item.get('slug') or 'untitled'
    path = output_base / f"{item['uuid']}_{item_slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_artefact_path(
    output_base: Path,
    item: Dict[str, Any],
    artefact: Dict[str, Any]
) -> Path:
    """
    Get artefact-level directory path.

    Args:
        output_base: Base output directory
        item: Item dict with 'uuid' and 'slug' keys
        artefact: Artefact dict with 'uuid' and 'slug' keys

    Returns:
        Path to artefact directory
    """
    item_slug = item.get('slug') or 'untitled'
    artefact_slug = artefact.get('slug') or 'untitled'

    path = output_base / f"{item['uuid']}_{item_slug}"
    path = path / f"{artefact['uuid']}_{artefact_slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_analysis_path(
    output_base: Path,
    item: Dict[str, Any],
    artefact: Dict[str, Any],
    analysis: Dict[str, Any]
) -> Path:
    """
    Get analysis-level directory path.

    Args:
        output_base: Base output directory
        item: Item dict with 'uuid' and 'slug' keys
        artefact: Artefact dict with 'uuid' and 'slug' keys
        analysis: Analysis dict with 'uuid' and 'slug' keys

    Returns:
        Path to analysis directory
    """
    item_slug = item.get('slug') or 'untitled'
    artefact_slug = artefact.get('slug') or 'untitled'
    analysis_slug = analysis.get('slug') or 'untitled'

    path = output_base / f"{item['uuid']}_{item_slug}"
    path = path / f"{artefact['uuid']}_{artefact_slug}"
    path = path / f"{analysis['uuid']}_{analysis_slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_path_components(path: Path) -> Optional[Dict[str, str]]:
    """
    Parse hierarchical path back into components.

    Args:
        path: Path following the hierarchical structure

    Returns:
        Dict with 'item_uuid', 'artefact_uuid', 'analysis_uuid', etc.
        or None if path doesn't match expected structure

    Examples:
        >>> parse_path_components(
        ...     Path('/data/outputs/abc123_risc-os/def456_disc-1/ghi789_file-listing')
        ... )
        {
            'item_uuid': 'abc123',
            'item_slug': 'risc-os',
            'artefact_uuid': 'def456',
            'artefact_slug': 'disc-1',
            'analysis_uuid': 'ghi789',
            'analysis_slug': 'file-listing'
        }
    """
    parts = path.parts

    if len(parts) < 3:
        return None

    components = {}

    # Parse in reverse: analysis, artefact, item
    try:
        # Analysis
        analysis_part = parts[-1]
        if '_' in analysis_part:
            uuid, slug = analysis_part.split('_', 1)
            components['analysis_uuid'] = uuid
            components['analysis_slug'] = slug

        # Artefact
        if len(parts) >= 2:
            artefact_part = parts[-2]
            if '_' in artefact_part:
                uuid, slug = artefact_part.split('_', 1)
                components['artefact_uuid'] = uuid
                components['artefact_slug'] = slug

        # Item
        if len(parts) >= 3:
            item_part = parts[-3]
            if '_' in item_part:
                uuid, slug = item_part.split('_', 1)
                components['item_uuid'] = uuid
                components['item_slug'] = slug

        # Partition (if present)
        if len(parts) >= 4 and parts[-1].startswith('partition_'):
            partition_part = parts[-1]
            partition_parts = partition_part.split('_', 2)
            if len(partition_parts) >= 3:
                components['partition_index'] = int(partition_parts[1])
                components['partition_slug'] = partition_parts[2]

    except (ValueError, IndexError):
        return None

    return components if components else None
