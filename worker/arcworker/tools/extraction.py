"""
File extraction tools.

Tools for listing and extracting files from disk images.
Supports:
- 7z - DOS/FAT, ISO, and many archive formats
- Disc Image Manager - Acorn DFS/ADFS filesystems
"""

import os
import re
import shutil
import tempfile
from pathlib import Path

from .base import run_tool_with_output

# Debugging option: if True, scripts and output files will not be deleted.
_DEBUG_KEEP_OUTFILES = False


def extract_acorn_disc_image_manager(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from Acorn DFS/ADFS disc image using Disc Image Manager.
    Creates INF files for metadata.

    Args:
        input_path: Path to Acorn disc image
        output_dir: Directory for extracted files

    Returns:
        Result dict with success status, file count, output directory, and process_output
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create DIM script
    script_content = f"""insert {input_path}
report
chdir {output_dir}
config CreateINF true
extract *
exit
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.dim', delete=False) as f:
        f.write(script_content)
        script_path = f.name

    try:
        cmd = [
            'DiscImageManager',
            '-s', script_path
        ]
        result, process_output = run_tool_with_output(cmd)

        # Count extracted files
        extracted_files = list(output_dir.rglob('*'))
        file_count = sum(1 for f in extracted_files if f.is_file() and not f.suffix == '.inf')

        if file_count > 0:
            return {
                'success': True,
                'tool': 'DiscImageManager',
                'output_dir': str(output_dir),
                'file_count': file_count,
                'summary': f'Extracted {file_count} files from Acorn disc image',
                'process_output': process_output
            }

        return {
            'success': False,
            'tool': 'DiscImageManager',
            'error': 'No files extracted - may not be Acorn format',
            'process_output': process_output
        }

    finally:
        if not _DEBUG_KEEP_OUTFILES:
            os.unlink(script_path)


def extract_dos_7z(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from DOS/FAT disc image using 7z.
    Works for FAT12/16/32 filesystems.

    Args:
        input_path: Path to DOS/FAT disc image
        output_dir: Directory for extracted files

    Returns:
        Result dict with success status, file count, output directory, and process_output
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        '7z', 'x',
        f'-o{output_dir}',
        '-y',  # Yes to all
        str(input_path)
    ]
    result, process_output = run_tool_with_output(cmd)

    # Count extracted files
    extracted_files = list(output_dir.rglob('*'))
    file_count = sum(1 for f in extracted_files if f.is_file())

    if file_count > 0:
        return {
            'success': True,
            'tool': '7z',
            'output_dir': str(output_dir),
            'file_count': file_count,
            'summary': f'Extracted {file_count} files from DOS image',
            'process_output': process_output
        }

    return {
        'success': False,
        'tool': '7z',
        'error': result.stderr.decode()[:1000] if result.returncode != 0 else 'No files extracted',
        'process_output': process_output
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


def _parse_acorn_filename(filename: str) -> tuple[str, str | None]:
    """
    Parse Acorn filename to extract the true filename and filetype.

    Acorn files extracted by DIM have format: filename,xxx where xxx is the
    filetype in hex (e.g., !Run,feb means filename "!Run" with filetype 0xFEB).

    Args:
        filename: The filename as extracted by DIM

    Returns:
        Tuple of (true_filename, filetype_hex_or_none)
    """
    # Match filename,xxx pattern where xxx is 1-3 hex digits
    match = re.match(r'^(.+),([0-9a-fA-F]{1,3})$', filename)
    if match:
        return match.group(1), match.group(2).lower()
    return filename, None


def _parse_dim_report(output: str) -> dict:
    """
    Parse Disc Image Manager report output to extract disc metadata.

    Args:
        output: DIM process output containing report data

    Returns:
        Dict with 'disc_name' and 'container_format' if found
    """
    result = {}

    for line in output.split('\n'):
        line = line.strip()

        # Extract disc name (e.g., "Disc Name: TheHacker")
        if line.startswith('Disc Name:'):
            disc_name = line.split(':', 1)[1].strip()
            if disc_name:
                result['disc_name'] = disc_name

        # Extract container format (e.g., "Container format: Acorn ADFS E")
        elif line.startswith('Container format:'):
            container_format = line.split(':', 1)[1].strip()
            if container_format:
                result['container_format'] = container_format

    return result


def list_files_dim(input_path: Path) -> dict:
    """
    List files in an Acorn DFS/ADFS disc image using Disc Image Manager.
    Extracts to a temp directory to enumerate files and parse Acorn filetypes.

    Args:
        input_path: Path to Acorn disc image

    Returns:
        Result dict with success status, file list, count, and process_output.
        Each file entry includes 'path', 'size', and optionally 'filetype'.
        Also includes 'disc_name' and 'container_format' if available.
    """
    from ..utils.text import sanitize_path

    # Create a temp directory for extraction
    temp_dir = tempfile.mkdtemp(prefix='dim_list_')

    # Create DIM script to extract files (include report command)
    script_content = f"""insert {input_path}
report
chdir {temp_dir}
extract *
exit
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.dim', delete=False) as f:
        f.write(script_content)
        script_path = f.name

    try:
        cmd = [
            'DiscImageManager',
            '-s', script_path
        ]
        result, process_output = run_tool_with_output(cmd)

        # Enumerate extracted files using Python
        files = []
        temp_path = Path(temp_dir)

        for file_path in temp_path.rglob('*'):
            if not file_path.is_file():
                continue

            # Skip .inf metadata files
            if file_path.suffix == '.inf':
                continue

            # Get relative path from temp directory
            rel_path = file_path.relative_to(temp_path)

            # Parse Acorn filename to extract filetype
            true_name, filetype = _parse_acorn_filename(file_path.name)

            # Reconstruct path with true filename (without filetype suffix)
            if filetype and len(rel_path.parts) > 1:
                display_path = str(Path(*rel_path.parts[:-1]) / true_name)
            elif filetype:
                display_path = true_name
            else:
                display_path = str(rel_path)

            # Sanitize path for UTF-8 database storage (handles Acorn Latin-1, surrogates)
            display_path = sanitize_path(display_path)

            file_size = file_path.stat().st_size

            file_entry = {
                'path': display_path,
                'size': file_size,
            }

            # Detect ADFS directories (typically 2KB/2048 bytes)
            # RISC OS directories can also have filetype 'ddc' or attributes with 'D'
            is_directory = (
                file_size == 2048 or  # ADFS directory size
                filetype == 'ddc'     # RISC OS directory filetype
            )

            if is_directory:
                file_entry['is_directory'] = True

            # Store RISC OS filetype (hex string like '3fb') for archive detection
            if filetype:
                file_entry['risc_os_filetype'] = filetype

            files.append(file_entry)

        # Parse DIM report output for disc metadata
        metadata = _parse_dim_report(process_output)

        if files:
            result_dict = {
                'success': True,
                'tool': 'DiscImageManager',
                'files': files,
                'file_count': len(files),
                'summary': f'Found {len(files)} files in Acorn disc image',
                'process_output': process_output
            }
            # Include disc metadata if available
            if metadata:
                result_dict.update(metadata)
            return result_dict

        return {
            'success': False,
            'tool': 'DiscImageManager',
            'error': 'No files found - may not be Acorn format',
            'process_output': process_output
        }

    finally:
        if not _DEBUG_KEEP_OUTFILES:
            os.unlink(script_path)
            shutil.rmtree(temp_dir, ignore_errors=True)


def list_files_7z(input_path: Path) -> dict:
    """
    List files in an image using 7z without extracting.
    Returns structured file listing.

    Args:
        input_path: Path to image file

    Returns:
        Result dict with success status, file list, count, and process_output
    """
    from ..utils.text import sanitize_path

    cmd = [
        '7z', 'l',
        '-slt',  # Technical listing format
        str(input_path)
    ]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'tool': '7z',
            'error': result.stderr.decode(errors='replace')[:1000],
            'process_output': process_output
        }

    # Parse 7z output (handle encoding issues)
    files = []
    current_file = {}

    for line in result.stdout.decode(errors='surrogateescape').split('\n'):
        line = line.strip()
        if line.startswith('Path = '):
            if current_file and 'path' in current_file:
                files.append(current_file)
            # Sanitize path for UTF-8 database storage
            path = sanitize_path(line[7:])
            current_file = {'path': path}
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
        'summary': f'Found {len(files)} files',
        'process_output': process_output
    }


def convert_fcfs_to_raw(input_path: Path, output_path: Path) -> dict:
    """
    Convert FCFS (Filecore) disk image to raw sector image using fcfs2raw.

    Args:
        input_path: Path to FCFS image file
        output_path: Path to output raw image file

    Returns:
        Result dict with success status
    """
    cmd = ['fcfs2raw', '-v', str(input_path), str(output_path)]
    result, process_output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'fcfs2raw failed with exit code {result.returncode}',
            'tool': 'fcfs2raw',
            'process_output': process_output
        }

    return {
        'success': True,
        'tool': 'fcfs2raw',
        'output_path': str(output_path),
        'summary': 'FCFS image converted to raw sector format',
        'process_output': process_output
    }
