"""
Analysis tools package.

Contains wrappers for external analysis tools.
"""

from .base import (
    run_tool,
    run_tool_with_output,
    get_process_output,
    compute_file_hash,
    tool_result,
    run_and_build_result,
    exception_result,
)
from .flux import (
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    flux_to_imd_hxcfe,
    flux_to_hfe_hxcfe,
    dfi_to_scp_hxcfe,
    a2r_to_scp_gw,
    sector_image_to_raw_greaseweazle,
)
from .extraction import (
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    extract_iso_7z,
    enumerate_extracted_files,
    process_inf_sidecars,
    parse_acorn_filename,
)
from .fs_iso_riscos import parse_iso_riscos_filetypes
from .iso9660 import parse_iso9660_pvd
from .partition import (
    detect_partitions_sfdisk,
    detect_acorn_adfs,
    detect_acorn_partitions,
    detect_format_file_cmd,
    detect_fat_filesystem,
    read_fat_volume_label,
)
from .archives import (
    extract_riscosarc,
    extract_tbafs,
    extract_xfiles,
    extract_zip,
    extract_zip_riscos,
    has_riscos_zip_metadata,
    extract_tar,
    extract_rar,
    extract_7z,
    decompress_single_file,
)
from .fs_riscos_armlock import detect_armlock, remove_armlock
from .images_acorn import convert_sprite, convert_draw
from .riscos_module import decode_module, HelpParseError, ModuleParseError

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
