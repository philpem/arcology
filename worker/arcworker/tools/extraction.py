"""
File extraction tools.

Tools for listing and extracting files from disk images.
Supports:
- 7z - DOS/FAT, ISO, and many archive formats
- Disc Image Manager - Acorn DFS/ADFS filesystems
"""

import os
import tempfile
from pathlib import Path

from .base import run_tool


def extract_acorn_disc_image_manager(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from Acorn DFS/ADFS disc image using Disc Image Manager.
    Creates INF files for metadata.

    Args:
        input_path: Path to Acorn disc image
        output_dir: Directory for extracted files

    Returns:
        Result dict with success status, file count, and output directory
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create DIM script
    script_content = f"""insert {input_path}
report
chdir {output_dir}
config CreateINF true
extract * {output_dir}
exit
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.dim', delete=False) as f:
        f.write(script_content)
        script_path = f.name

    try:
        result = run_tool([
            'xvfb-run',
            'DiscImageManager',
            '-c', script_path
        ])

        # Count extracted files
        extracted_files = list(output_dir.rglob('*'))
        file_count = sum(1 for f in extracted_files if f.is_file() and not f.suffix == '.inf')

        if file_count > 0:
            return {
                'success': True,
                'tool': 'DiscImageManager',
                'output_dir': str(output_dir),
                'file_count': file_count,
                'summary': f'Extracted {file_count} files from Acorn disc image'
            }

        return {
            'success': False,
            'tool': 'DiscImageManager',
            'error': 'No files extracted - may not be Acorn format'
        }

    finally:
        os.unlink(script_path)


def extract_dos_7z(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from DOS/FAT disc image using 7z.
    Works for FAT12/16/32 filesystems.

    Args:
        input_path: Path to DOS/FAT disc image
        output_dir: Directory for extracted files

    Returns:
        Result dict with success status, file count, and output directory
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    result = run_tool([
        '7z', 'x',
        f'-o{output_dir}',
        '-y',  # Yes to all
        str(input_path)
    ])

    # Count extracted files
    extracted_files = list(output_dir.rglob('*'))
    file_count = sum(1 for f in extracted_files if f.is_file())

    if file_count > 0:
        return {
            'success': True,
            'tool': '7z',
            'output_dir': str(output_dir),
            'file_count': file_count,
            'summary': f'Extracted {file_count} files from DOS image'
        }

    return {
        'success': False,
        'tool': '7z',
        'error': result.stderr.decode()[:1000] if result.returncode != 0 else 'No files extracted'
    }


def extract_iso_7z(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from ISO image using 7z.

    Args:
        input_path: Path to ISO image
        output_dir: Directory for extracted files

    Returns:
        Result dict with success status, file count, and output directory
    """
    return extract_dos_7z(input_path, output_dir)  # Same process


def list_files_7z(input_path: Path) -> dict:
    """
    List files in an image using 7z without extracting.
    Returns structured file listing.

    Args:
        input_path: Path to image file

    Returns:
        Result dict with success status, file list, and count
    """
    result = run_tool([
        '7z', 'l',
        '-slt',  # Technical listing format
        str(input_path)
    ])

    if result.returncode != 0:
        return {
            'success': False,
            'tool': '7z',
            'error': result.stderr.decode()[:1000]
        }

    # Parse 7z output
    files = []
    current_file = {}

    for line in result.stdout.decode().split('\n'):
        line = line.strip()
        if line.startswith('Path = '):
            if current_file and 'path' in current_file:
                files.append(current_file)
            current_file = {'path': line[7:]}
        elif line.startswith('Size = '):
            try:
                current_file['size'] = int(line[7:])
            except ValueError:
                pass
        elif line.startswith('Modified = '):
            current_file['modified'] = line[11:]
        elif line.startswith('CRC = '):
            current_file['crc32'] = line[6:].lower()

    if current_file and 'path' in current_file:
        files.append(current_file)

    # Filter out directory entries (size 0 or no size)
    files = [f for f in files if f.get('size', 0) > 0]

    return {
        'success': True,
        'tool': '7z',
        'files': files,
        'file_count': len(files),
        'summary': f'Found {len(files)} files'
    }
