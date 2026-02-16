"""
Partition detection tools.

Tools for detecting partitions and filesystems in raw disc images.
Supports:
- sfdisk - Standard MBR/GPT partition tables
- ADFS signature detection - Acorn ADFS filesystem heuristics
- file command - Generic format identification
"""

import json
from pathlib import Path

from .base import run_tool_with_output


def detect_partitions_sfdisk(input_path: Path) -> dict:
    """
    Detect partitions using sfdisk (handles MBR and GPT).

    Args:
        input_path: Path to raw disc image

    Returns:
        Result dict with success status, partitions list, and process_output
    """
    try:
        cmd = ['sfdisk', '--json', str(input_path)]
        result, process_output = run_tool_with_output(cmd, timeout=30)
    except FileNotFoundError:
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': 'sfdisk not installed',
        }

    if result.returncode != 0:
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': f'sfdisk failed (exit {result.returncode})',
            'process_output': process_output
        }

    try:
        data = json.loads(result.stdout)
        table = data.get('partitiontable', {})
        partitions = []

        for i, part in enumerate(table.get('partitions', [])):
            partitions.append({
                'index': i,
                'start_sector': part.get('start'),
                'size_sectors': part.get('size'),
                'type': part.get('type', 'unknown'),
                'node': part.get('node', ''),
            })

        return {
            'success': len(partitions) > 0,
            'tool': 'sfdisk',
            'table_type': table.get('label', 'unknown'),
            'sector_size': table.get('sectorsize', 512),
            'partitions': partitions,
            'process_output': process_output
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': f'Failed to parse sfdisk output: {e}',
            'process_output': process_output
        }


def detect_acorn_adfs(input_path: Path) -> dict:
    """
    Detect Acorn ADFS filesystem by checking for known signatures.

    Checks for:
    - ADFS boot block checksum (sum of first 512 bytes == 0 mod 256)
    - "Hugo" signature (old-format ADFS directories: ADFS-S, M, L, D)
    - "SBPr" / "Nick" signatures (new-format ADFS directories: ADFS-E, E+, F, F+)

    Args:
        input_path: Path to raw disc image

    Returns:
        Result dict with detection info
    """
    try:
        file_size = input_path.stat().st_size
        # Read first 16KB - enough to cover boot block and start of root directory
        read_size = min(file_size, 16384)

        with open(input_path, 'rb') as f:
            header = f.read(read_size)

        signatures = []
        adfs_variant = None

        # Check ADFS boot block checksum (new-map: D, E, E+, F, F+)
        # The sum of all 512 bytes in the boot block should be 0 mod 256
        if len(header) >= 512:
            boot_checksum = sum(header[0:512]) & 0xFF
            if boot_checksum == 0 or boot_checksum_2 == 0:
                signatures.append('Valid ADFS boot block checksum (sector 0)')
                adfs_variant = 'new_map'

        # F-format (hard discs) has the block at disc address 0xC00
        if len(header) >= 0xC00+512:
            boot_checksum = sum(header[0xC00:0xC00:512]) & 0xFF
            if boot_checksum == 0 or boot_checksum_2 == 0:
                signatures.append('Valid ADFS boot block checksum (disc address &C00)')
                adfs_variant = 'new_map'

        # Check for old-format directory signature "Hugo"
        # Old-map root directory starts at byte 0x200 (sector 2 at 256 bytes/sector)
        # "Hugo" appears at end of directory: offset depends on directory size
        # Common offsets: 0x4CB (small dir), 0x6CB (large dir)
        for offset in [0x4CB, 0x6CB]:
            if offset + 4 <= len(header) and header[offset:offset + 4] == b"Hugo":
                signatures.append(f'"Hugo" at 0x{offset:X} (old-format directory)')
                adfs_variant = 'old_map'

        # Check for new-format directory header "SBPr"
        # Scan sector-aligned offsets in the first 16KB
        for offset in range(0, len(header) - 4, 512):
            if header[offset:offset + 4] == b"SBPr":
                signatures.append(f'"SBPr" at 0x{offset:X} (new-format directory)')
                if not adfs_variant:
                    adfs_variant = 'new_map'
                break

        # Check for new-format directory tail "Nick"
        for offset in [0x4CB, 0x6CB, 0x7FF, 0xBFF]:
            if offset + 4 <= len(header) and header[offset:offset + 4] == b"Nick":
                signatures.append(f'"Nick" at 0x{offset:X} (new-format directory tail)')
                if not adfs_variant:
                    adfs_variant = 'new_map'

        return {
            'adfs_detected': len(signatures) > 0,
            'adfs_variant': adfs_variant,
            'disc_size': file_size,
            'signatures': signatures,
        }

    except Exception as e:
        return {
            'adfs_detected': False,
            'error': str(e),
        }


def detect_format_file_cmd(input_path: Path) -> dict:
    """
    Use the 'file' command to identify disc image format.

    Args:
        input_path: Path to disc image

    Returns:
        Result dict with file type info and process_output
    """
    try:
        cmd = ['file', '-b', str(input_path)]
        result, process_output = run_tool_with_output(cmd, timeout=10)
    except FileNotFoundError:
        return {
            'success': False,
            'tool': 'file',
            'file_type': '',
            'error': 'file command not installed',
        }

    file_type = result.stdout.decode(errors='replace').strip() if result.returncode == 0 else ''

    return {
        'success': result.returncode == 0,
        'tool': 'file',
        'file_type': file_type,
        'process_output': process_output
    }
