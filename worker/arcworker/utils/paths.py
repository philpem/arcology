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

# vim: ts=4 sw=4 et
