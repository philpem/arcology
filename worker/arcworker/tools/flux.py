"""
Flux image analysis tools.

Tools for visualising and converting flux-level disk images.
Supports:
- Fluxfox (imgviz) - Detailed flux visualisation
- HxCFE - Flux visualisation and format conversion
- Greaseweazle - Sector image conversion
"""

from pathlib import Path

from shared.enums import ArtefactType
from .base import run_tool_with_output


# Map (filesystem, cylinders, heads, sectors_per_track, sector_size) → gw format name.
# Format names verified against Greaseweazle diskdefs at commit 26690f89.
# SPT values are authoritative (from boot structures), not from IMD headers.
_GW_FORMAT_MAP: dict[tuple, str] = {
    ('dfs',      40, 1, 10, 256):  'acorn.dfs.ss',
    ('dfs',      40, 2, 10, 256):  'acorn.dfs.ds',
    ('dfs',      80, 1, 10, 256):  'acorn.dfs.ss80',
    ('dfs',      80, 2, 10, 256):  'acorn.dfs.ds80',
    ('adfs_old', 40, 1, 16, 256):  'acorn.adfs.160',
    ('adfs_old', 80, 1, 16, 256):  'acorn.adfs.320',
    ('adfs_old', 80, 2, 16, 256):  'acorn.adfs.640',
    ('adfs_d',   80, 2,  5, 1024): 'acorn.adfs.800',
    ('adfs_e',   80, 2,  5, 1024): 'acorn.adfs.800',   # same physical format as D
    ('adfs_f',   80, 2, 10, 1024): 'acorn.adfs.1600',
    ('fat',      40, 1,  8, 512):  'ibm.160',
    ('fat',      40, 1,  9, 512):  'ibm.180',
    ('fat',      40, 2,  8, 512):  'ibm.320',
    ('fat',      40, 2,  9, 512):  'ibm.360',
    ('fat',      80, 2,  9, 512):  'ibm.720',
    ('fat',      80, 2, 15, 512):  'ibm.1200',
    ('fat',      80, 2, 18, 512):  'ibm.1440',
    ('fat',      80, 2, 21, 512):  'ibm.1680',
    ('fat',      80, 2, 36, 512):  'ibm.2880',
}


def _geometry_to_gw_format(
    filesystem: str,
    cylinders: int,
    heads: int,
    sectors_per_track: int,
    sector_size: int,
    encoding: str = '',
) -> str | None:
    """Return a Greaseweazle format name for the given geometry, or None."""
    return _GW_FORMAT_MAP.get((filesystem, cylinders, heads, sectors_per_track, sector_size))


def flux_visualisation_fluxfox(input_path: Path, output_path: Path) -> dict:
    """
    Generate flux visualisation using Fluxfox imgviz.
    Produces a detailed flux graph PNG.

    Args:
        input_path: Path to flux image (SCP, etc.)
        output_path: Path for output PNG

    Returns:
        Result dict with success status, tool name, output details, and process_output
    """
    cmd = [
        'imgviz',
        '-i', str(input_path),
        f'-o={output_path}',
        #'--angle=2.88',
        '--hole_ratio=0.3',
        '--index_hole',
        '--data',
        '--metadata',
        '--decode',
        '--resolution=2048',
        '--ss=4',
        '--rasterize_data'
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'fluxfox/imgviz',
            'output_path': str(output_path),
            'summary': 'Flux visualisation generated with Fluxfox',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'fluxfox/imgviz',
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }


def flux_visualisation_hxcfe(input_path: Path, output_path: Path) -> dict:
    """
    Generate flux visualisation using HxC Floppy Emulator.
    Alternative visualisation style.

    Args:
        input_path: Path to flux image
        output_path: Path for output PNG

    Returns:
        Result dict with success status, tool name, output details, and process_output
    """
    cmd = [
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:PNG_DISK_IMAGE',
        f'-foutput:{output_path}'
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'summary': 'Flux visualisation generated with HxCFE',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }


def flux_to_imd_hxcfe(input_path: Path, output_path: Path) -> dict:
    """
    Convert flux image (SCP) to ImageDisk format using HxCFE.

    Args:
        input_path: Path to flux image
        output_path: Path for output IMD file

    Returns:
        Result dict with success status, output type, and process_output
    """
    cmd = [
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:IMD_IMG',
        f'-foutput:{output_path}'
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'output_type': ArtefactType.IMD.value,
            'summary': 'Converted to ImageDisk format',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }


def flux_to_hfe_hxcfe(input_path: Path, output_path: Path) -> dict:
    """
    Convert flux image (SCP) to HFE format using HxCFE.

    Args:
        input_path: Path to flux image
        output_path: Path for output HFE file

    Returns:
        Result dict with success status, output type, and process_output
    """
    cmd = [
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:HXC_HFEV3',
        f'-foutput:{output_path}'
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'output_type': ArtefactType.HFE.value,
            'summary': 'Converted to HFE format',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }


def sector_image_to_raw_greaseweazle(
    input_path: Path,
    output_path: Path,
    gw_format: str = 'ibm.scan',
) -> dict:
    """
    Convert sector image (IMD, HFE, SCP) to raw sector image using Greaseweazle.
    Greaseweazle is preferred as it fills in bad sectors.

    Args:
        input_path: Path to sector/flux image
        output_path: Path for output raw IMG file
        gw_format: Greaseweazle format name (default 'ibm.scan')

    Returns:
        Result dict with success status, output type, gw_format, and process_output
    """
    cmd = [
        'gw', 'convert',
        '--format', gw_format,
        str(input_path),
        str(output_path)
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'greaseweazle',
            'output_path': str(output_path),
            'output_type': ArtefactType.RAW_SECTOR.value,
            'gw_format': gw_format,
            'summary': 'Converted to raw sector image (bad sectors filled)',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'greaseweazle',
        'gw_format': gw_format,
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }

# vim: ts=4 sw=4 et
