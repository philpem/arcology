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
from typing import Any


def _slug(value: dict[str, Any], default: str = 'untitled') -> str:
    """Return a safe slug fallback for a path segment dict."""
    return value.get('slug') or default


def _segment(value: dict[str, Any]) -> str:
    """Build a standard UUID_slug path segment."""
    return f"{value['uuid']}_{_slug(value)}"


def _ensure_dir(path: Path) -> Path:
    """Create a path if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _base_output_path(output_base: Path, item: dict[str, Any]) -> Path:
    """Return the item-level output directory path."""
    return output_base / _segment(item)


def _artefact_output_path(output_base: Path, item: dict[str, Any], artefact: dict[str, Any]) -> Path:
    """Return the artefact-level output directory path."""
    return _base_output_path(output_base, item) / _segment(artefact)


def _analysis_output_path(
    output_base: Path,
    item: dict[str, Any],
    artefact: dict[str, Any],
    analysis: dict[str, Any],
) -> Path:
    """Return the analysis-level output directory path."""
    analysis_type = analysis.get('analysis_type', 'untitled')
    segment = f"{analysis['uuid']}_{_slug(analysis, analysis_type)}"
    return _artefact_output_path(output_base, item, artefact) / segment


def get_output_path(
    output_base: Path,
    item: dict[str, Any],
    artefact: dict[str, Any],
    analysis: dict[str, Any],
    partition: dict[str, Any] | None = None
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
        Path object for output directory (not created; caller is responsible)

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
    path = _analysis_output_path(output_base, item, artefact, analysis)

    # Add partition subdirectory if specified
    if partition:
        partition_index = partition.get('partition_index', 0)
        partition_slug = _slug(partition, str(partition_index))
        path = path / f"partition_{partition_index}_{partition_slug}"

    return path


def get_item_path(output_base: Path, item: dict[str, Any]) -> Path:
    """
    Get item-level directory path.

    Args:
        output_base: Base output directory
        item: Item dict with 'uuid' and 'slug' keys

    Returns:
        Path to item directory
    """
    return _ensure_dir(_base_output_path(output_base, item))


def get_artefact_path(
    output_base: Path,
    item: dict[str, Any],
    artefact: dict[str, Any]
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
    return _ensure_dir(_artefact_output_path(output_base, item, artefact))


def get_analysis_path(
    output_base: Path,
    item: dict[str, Any],
    artefact: dict[str, Any],
    analysis: dict[str, Any]
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
    return _ensure_dir(_analysis_output_path(output_base, item, artefact, analysis))

# vim: ts=4 sw=4 et
