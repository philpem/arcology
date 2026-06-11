"""
Flux image analysis tools.

Tools for visualising and converting flux-level disk images.
Supports:
- Fluxfox (imgviz) - Detailed flux visualisation
- HxCFE - Flux visualisation and format conversion
- Greaseweazle - Sector image conversion
"""

import tempfile
from pathlib import Path
from arcology_shared.enums import ArtefactType
from .base import run_and_build_result, run_tool_with_output, tool_result

# Map (filesystem, cylinders, heads, sectors_per_track, sector_size) → gw format name.
# Format names verified against Greaseweazle diskdefs at commit 26690f89.
# SPT values are authoritative (from boot structures), not from IMD headers.
# All ADFS variants use filesystem='adfs'; the geometry alone is sufficient to
# select the correct format — no sub-type (adfs_d/e/f/old) is needed.
_GW_FORMAT_MAP: dict[tuple, str] = {
    ('dfs',  40, 1, 10, 256):  'acorn.dfs.ss',
    ('dfs',  40, 2, 10, 256):  'acorn.dfs.ds',
    ('dfs',  80, 1, 10, 256):  'acorn.dfs.ss80',
    ('dfs',  80, 2, 10, 256):  'acorn.dfs.ds80',
    ('adfs', 40, 1, 16, 256):  'acorn.adfs.160',
    ('adfs', 80, 1, 16, 256):  'acorn.adfs.320',
    ('adfs', 80, 2, 16, 256):  'acorn.adfs.640',
    ('adfs', 80, 2,  5, 1024): 'acorn.adfs.800',
    ('adfs', 80, 2, 10, 1024): 'acorn.adfs.1600',
    ('fat',  40, 1,  8, 512):  'ibm.160',
    ('fat',  40, 1,  9, 512):  'ibm.180',
    ('fat',  40, 2,  8, 512):  'ibm.320',
    ('fat',  40, 2,  9, 512):  'ibm.360',
    ('fat',  80, 2,  9, 512):  'ibm.720',
    ('fat',  80, 2, 15, 512):  'ibm.1200',
    ('fat',  80, 2, 18, 512):  'ibm.1440',
    ('fat',  80, 2, 21, 512):  'ibm.1680',
    ('fat',  80, 2, 36, 512):  'ibm.2880',
}


def _geometry_to_gw_format(
    filesystem: str,
    cylinders: int,
    heads: int,
    sectors_per_track: int,
    sector_size: int,
    encoding: str = '',
    **_ignored,
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
    return run_and_build_result(
        cmd,
        tool='fluxfox/imgviz',
        output_path=output_path,
        summary='Flux visualisation generated with Fluxfox',
    )


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
    return run_and_build_result(
        cmd,
        tool='hxcfe',
        output_path=output_path,
        summary='Flux visualisation generated with HxCFE',
    )


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
    return run_and_build_result(
        cmd,
        tool='hxcfe',
        output_path=output_path,
        summary='Converted to ImageDisk format',
        output_type=ArtefactType.IMD.value,
    )


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
    return run_and_build_result(
        cmd,
        tool='hxcfe',
        output_path=output_path,
        summary='Converted to HFE format',
        output_type=ArtefactType.HFE.value,
    )


def dfi_to_scp_hxcfe(input_path: Path, output_path: Path, clock_mhz: int | None = None) -> dict:
    """
    Convert a DiscFerret DFI flux image to SuperCard Pro SCP format using HxCFE.

    When clock_mhz is provided, a small hxcfe script containing
    'set DFILOADER_SAMPLE_FREQUENCY_MHZ N' is written to a temp file and
    passed via -script:, which hxcfe executes before processing -finput:.
    The parameter cannot be set via a command-line flag directly.

    Args:
        input_path: Path to DFI file
        output_path: Path for output SCP file
        clock_mhz: Optional sample frequency override in MHz

    Returns:
        Result dict with success status, output type, and process_output
    """
    if clock_mhz is not None:
        # hxcfe processes -script: before -finput:, so 'set' in the script takes
        # effect before the DFI loader reads the file.
        script = f'set DFILOADER_SAMPLE_FREQUENCY_MHZ {int(clock_mhz)}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.hxcfe', delete=False) as tf:
            tf.write(script)
            script_path = tf.name
        try:
            cmd = [
                'hxcfe',
                f'-script:{script_path}',
                f'-finput:{input_path}',
                '-conv:SCP_FLUX_STREAM',
                f'-foutput:{output_path}',
            ]
            result, process_output = run_tool_with_output(cmd)
            process_output['script'] = script
        finally:
            Path(script_path).unlink(missing_ok=True)
    else:
        cmd = [
            'hxcfe',
            f'-finput:{input_path}',
            '-conv:SCP_FLUX_STREAM',
            f'-foutput:{output_path}',
        ]
        result, process_output = run_tool_with_output(cmd)

    if result.returncode == 0 and output_path.exists():
        return tool_result(
            True, tool='hxcfe',
            output_path=str(output_path),
            output_type=ArtefactType.SCP.value,
            summary='Converted DFI to SCP flux stream',
            process_output=process_output,
        )

    return tool_result(
        False, tool='hxcfe',
        error=result.stderr.decode(errors='replace')[:1000],
        process_output=process_output,
    )


def a2r_to_scp_gw(input_path: Path, output_path: Path) -> dict:
    """
    Convert an Applesauce A2R flux image to SuperCard Pro SCP format using Greaseweazle.

    Greaseweazle handles A2R natively and auto-detects the clock frequency,
    so no frequency override or script is needed.

    Args:
        input_path: Path to A2R file
        output_path: Path for output SCP file

    Returns:
        Result dict with success status, output type, and process_output
    """
    cmd = [
        'gw', 'convert',
        str(input_path),
        str(output_path),
    ]
    return run_and_build_result(
        cmd,
        tool='greaseweazle',
        output_path=output_path,
        summary='Converted A2R to SCP flux stream',
        output_type=ArtefactType.SCP.value,
    )


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
    return run_and_build_result(
        cmd,
        tool='greaseweazle',
        output_path=output_path,
        summary='Converted to raw sector image (bad sectors filled)',
        output_type=ArtefactType.RAW_SECTOR.value,
        gw_format=gw_format,
    )

def sector_image_to_raw_greaseweazle_one_side(
    input_path: Path,
    output_path: Path,
    gw_format: str,
    head: int,
    cylinders: int,
) -> dict:
    """
    Convert one physical side of a sector/flux image to a raw sector image.

    Uses Greaseweazle's --tracks selector to extract only the tracks belonging
    to the specified physical head, producing a single-sided raw IMG.

    Greaseweazle maps input→output tracks positionally: a single-sided output
    format (e.g. acorn.dfs.ss80) only ever requests logical head 0, so the
    selector must always resolve to head 0.  Repeated 'h=' tokens do NOT build
    an input→output remap — the last one wins — so 'h=1:h=0' silently collapses
    to plain head 0 and reads physical head 0 for both sides.  To lift *physical*
    head 1 into the head-0 output we use 'hswap', which flips the physical head
    inside greaseweazle's ch_to_pch() mapping while leaving the logical head at 0.

    Args:
        input_path: Source image (SCP, IMD, HFE)
        output_path: Output raw IMG file
        gw_format:  Greaseweazle format name (single-sided variant, e.g. 'acorn.dfs.ss80')
        head:       Physical head to extract (0 or 1)
        cylinders:  Number of cylinders (used to bound the track selector)

    Returns:
        Result dict with success status, output type, gw_format, and process_output
    """
    # Physical head 0 → logical head 0 directly; physical head 1 → logical head 0
    # via hswap (see the note above on why 'h=1:h=0' does not work).
    head_sel = 'hswap:h=0' if head == 1 else 'h=0'
    cmd = [
        'gw', 'convert',
        '--format', gw_format,
        '--tracks', f'c=0-{cylinders - 1}:{head_sel}',
        str(input_path),
        str(output_path),
    ]
    return run_and_build_result(
        cmd,
        tool='greaseweazle',
        output_path=output_path,
        summary=f'Converted head {head} to raw sector image (bad sectors filled)',
        output_type=ArtefactType.RAW_SECTOR.value,
        gw_format=gw_format,
    )


def scp_fix_track_density(input_path: Path, output_path: Path, heads: list[int] | None = None) -> dict:
    """
    Extract only even-indexed physical tracks from a double-stepped SCP,
    producing a corrected 40-track SCP using Greaseweazle.

    c=0-79:h=0-1:step=2 selects physical cylinders 0, 2, 4, ..., 78.
    Greaseweazle renumbers them 0-39 in the output file.
    """
    if heads:
        head_spec = str(heads[0]) if len(heads) == 1 else f"{min(heads)}-{max(heads)}"
    else:
        head_spec = "0-1"

    cmd = [
        'gw', 'convert',
        '--tracks', f'c=0-79:h={head_spec}:step=2',
        str(input_path),
        str(output_path),
    ]
    return run_and_build_result(
        cmd,
        tool='greaseweazle',
        output_path=output_path,
        summary='Extracted even-indexed tracks from double-stepped SCP',
        heads=heads if heads is not None else [0, 1],
    )

# vim: ts=4 sw=4 et
