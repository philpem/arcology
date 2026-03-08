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


def sector_image_to_raw_greaseweazle(input_path: Path, output_path: Path) -> dict:
    """
    Convert sector image (IMD, HFE, SCP) to raw sector image using Greaseweazle.
    Greaseweazle is preferred as it fills in bad sectors.

    Args:
        input_path: Path to sector/flux image
        output_path: Path for output raw IMG file

    Returns:
        Result dict with success status, output type, and process_output
    """
    cmd = [
        'gw', 'convert',
        '--format', 'ibm.scan',
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
            'summary': 'Converted to raw sector image (bad sectors filled)',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': 'greaseweazle',
        'error': result.stderr.decode()[:1000],
        'process_output': process_output
    }
