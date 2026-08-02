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
    list_zip_member_names,
    read_zip_comment,
)
from .armovie import ArmovieParseError, file_has_armovie_magic, parse_armovie_header
from .base import (
    FileTooLargeError,
    SectorReader,
    compute_file_hash,
    compute_file_hash_full,
    exception_result,
    get_process_output,
    open_sector_reader,
    read_file_capped,
    run_and_build_result,
    run_tool,
    run_tool_with_output,
    tool_result,
)
from .documents import word_to_text
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
    sector_image_to_raw_greaseweazle_one_side,
)
from .fs_iso_riscos import parse_iso_riscos_filetypes
from .fs_riscos_armlock import detect_armlock, remove_armlock
from .images_acorn import convert_draw, convert_replay_poster_sprite, convert_sprite
from .iso9660 import parse_iso9660_pvd
from .media_transcode import (
    extract_media_poster,
    probe_media,
    transcode_media_to_audio,
    transcode_media_to_mp4,
)
from .partition import (
    detect_acorn_adfs,
    detect_acorn_partitions,
    detect_fat_filesystem,
    detect_format_file_cmd,
    detect_partitions_sfdisk,
    read_fat_volume_label,
)
from .replay_transcode import transcode_armovie_to_audio, transcode_armovie_to_mp4
from .riscos_module import HelpParseError, ModuleParseError, decode_module

__all__ = [
    'ArmovieParseError',
    'FileTooLargeError',
    'HelpParseError',
    'ModuleParseError',
    'SectorReader',
    'a2r_to_scp_gw',
    'compute_file_hash',
    'compute_file_hash_full',
    'convert_draw',
    'convert_replay_poster_sprite',
    'convert_sprite',
    'decode_module',
    'decompress_single_file',
    'detect_acorn_adfs',
    'detect_acorn_partitions',
    'detect_armlock',
    'detect_fat_filesystem',
    'detect_format_file_cmd',
    'detect_partitions_sfdisk',
    'dfi_to_scp_hxcfe',
    'enumerate_extracted_files',
    'exception_result',
    'extract_7z',
    'extract_acorn_disc_image_manager',
    'extract_dos_7z',
    'extract_iso_7z',
    'extract_media_poster',
    'extract_rar',
    'extract_riscosarc',
    'extract_tar',
    'extract_tbafs',
    'extract_xfiles',
    'extract_zip',
    'extract_zip_riscos',
    'file_has_armovie_magic',
    'flux_to_hfe_hxcfe',
    'flux_to_imd_hxcfe',
    'flux_visualisation_fluxfox',
    'flux_visualisation_hxcfe',
    'get_process_output',
    'has_riscos_zip_metadata',
    'list_zip_member_names',
    'open_sector_reader',
    'parse_acorn_filename',
    'parse_armovie_header',
    'parse_iso9660_pvd',
    'parse_iso_riscos_filetypes',
    'probe_media',
    'process_inf_sidecars',
    'read_fat_volume_label',
    'read_file_capped',
    'read_zip_comment',
    'remove_armlock',
    'run_and_build_result',
    'run_tool',
    'run_tool_with_output',
    'sector_image_to_raw_greaseweazle',
    'sector_image_to_raw_greaseweazle_one_side',
    'tool_result',
    'transcode_armovie_to_audio',
    'transcode_armovie_to_mp4',
    'transcode_media_to_audio',
    'transcode_media_to_mp4',
    'word_to_text',
]

# vim: ts=4 sw=4 et
