"""
Analysis tools package.

Contains wrappers for external analysis tools.
"""

from .base import run_tool, run_tool_with_output, get_process_output, compute_file_hash
from .flux import (
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    flux_to_imd_hxcfe,
    flux_to_hfe_hxcfe,
    sector_image_to_raw_greaseweazle,
)
from .extraction import (
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    extract_iso_7z,
    enumerate_extracted_files,
)
from .partition import (
    detect_partitions_sfdisk,
    detect_acorn_adfs,
    detect_format_file_cmd,
)
from .archives import (
    extract_riscosarc,
    extract_tbafs,
    extract_zip,
    extract_tar,
    extract_rar,
    extract_7z,
    decompress_single_file,
)

__all__ = [
    'run_tool',
    'run_tool_with_output',
    'get_process_output',
    'compute_file_hash',
    'flux_visualisation_fluxfox',
    'flux_visualisation_hxcfe',
    'flux_to_imd_hxcfe',
    'flux_to_hfe_hxcfe',
    'sector_image_to_raw_greaseweazle',
    'extract_acorn_disc_image_manager',
    'extract_dos_7z',
    'extract_iso_7z',
    'enumerate_extracted_files',
    'detect_partitions_sfdisk',
    'detect_acorn_adfs',
    'detect_format_file_cmd',
    'extract_riscosarc',
    'extract_tbafs',
    'extract_zip',
    'extract_tar',
    'extract_rar',
    'extract_7z',
    'decompress_single_file',
]
