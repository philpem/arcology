"""
Analysis tools package.

Contains wrappers for external analysis tools.
"""

from .archives import (
    decompress_single_file,
    extract_7z,
    extract_rar,
    extract_riscosarc,
    extract_tar,
    extract_tbafs,
    extract_xfiles,
    extract_zip,
    extract_zip_riscos,
    has_riscos_zip_metadata,
)
from .base import (
    compute_file_hash,
    exception_result,
    get_process_output,
    run_and_build_result,
    run_tool,
    run_tool_with_output,
    tool_result,
)
from .extraction import (
    enumerate_extracted_files,
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    extract_iso_7z,
    parse_acorn_filename,
    process_inf_sidecars,
)
from .flux import (
    a2r_to_scp_gw,
    dfi_to_scp_hxcfe,
    flux_to_hfe_hxcfe,
    flux_to_imd_hxcfe,
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    sector_image_to_raw_greaseweazle,
)
from .fs_iso_riscos import parse_iso_riscos_filetypes
from .fs_riscos_armlock import detect_armlock, remove_armlock
from .images_acorn import convert_draw, convert_sprite
from .iso9660 import parse_iso9660_pvd
from .partition import (
    detect_acorn_adfs,
    detect_acorn_partitions,
    detect_fat_filesystem,
    detect_format_file_cmd,
    detect_partitions_sfdisk,
    read_fat_volume_label,
)
from .riscos_module import HelpParseError, ModuleParseError, decode_module

__all__ = [
    'run_tool',
    'run_tool_with_output',
    'get_process_output',
    'compute_file_hash',
    'tool_result',
    'run_and_build_result',
    'exception_result',
    'flux_visualisation_fluxfox',
    'flux_visualisation_hxcfe',
    'flux_to_imd_hxcfe',
    'flux_to_hfe_hxcfe',
    'dfi_to_scp_hxcfe',
    'a2r_to_scp_gw',
    'sector_image_to_raw_greaseweazle',
    'extract_acorn_disc_image_manager',
    'extract_dos_7z',
    'extract_iso_7z',
    'enumerate_extracted_files',
    'process_inf_sidecars',
    'parse_acorn_filename',
    'parse_iso_riscos_filetypes',
    'parse_iso9660_pvd',
    'detect_partitions_sfdisk',
    'detect_acorn_adfs',
    'detect_acorn_partitions',
    'detect_format_file_cmd',
    'detect_fat_filesystem',
    'read_fat_volume_label',
    'extract_riscosarc',
    'extract_tbafs',
    'extract_xfiles',
    'extract_zip',
    'extract_zip_riscos',
    'has_riscos_zip_metadata',
    'extract_tar',
    'extract_rar',
    'extract_7z',
    'decompress_single_file',
    'detect_armlock',
    'remove_armlock',
    'convert_sprite',
    'convert_draw',
    'decode_module',
    'HelpParseError',
    'ModuleParseError',
]

# vim: ts=4 sw=4 et
