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
from ..utils.text import normalize_extracted_filenames


def _decode_dos_cp850(data: bytes) -> str:
	"""
	Best-effort CP850 decode for Western European DOS filenames.

	7z passes raw FAT directory-entry bytes through to the Linux filesystem
	when extracting DOS disc images.  DOS systems in Western Europe typically
	used CP850; US-only systems used CP437.  CP850 is chosen as the default
	because this collection is UK/European-focused and the two encodings agree
	on the ASCII range (0x00–0x7F) and are close in 0x80–0xFF.

	If a disc is known to use a different code page, pass an explicit decoder
	to normalize_extracted_filenames() instead.
	"""
	return data.decode('cp850')


def _fix_dos_duplicate_extensions(root: Path) -> None:
	"""
	Rename files and directories that have a duplicated extension appended by 7z.

	Some versions of 7z, when extracting a VFAT (Long File Name) disk image,
	append the short 8.3 extension to the long filename.  This produces names
	like ``E002.ZIP.ZIP`` instead of ``E002.ZIP``.  The pattern is easy to
	detect: the file's stem already ends with the same extension as the file
	itself (case-insensitively).  In that case this function strips the extra
	suffix, restoring the intended name.

	The walk is bottom-up so that files inside a directory are fixed before
	the directory name itself is corrected.
	"""
	import logging
	log = logging.getLogger(__name__)

	for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
		dir_path = Path(dirpath)
		for name in filenames + dirnames:
			p = Path(name)
			suffix = p.suffix
			if not suffix:
				continue
			# Duplicate: stem already ends with the same extension (case-insensitive).
			# Example: 'E002.ZIP.ZIP' — stem 'E002.ZIP' ends with '.ZIP'.
			if not p.stem.lower().endswith(suffix.lower()):
				continue

			old_path = dir_path / name
			new_path = dir_path / p.stem  # strip the repeated extension

			if new_path.exists():
				log.warning(
					'_fix_dos_duplicate_extensions: skipping %r → %r: target exists',
					str(old_path), str(new_path),
				)
				continue

			try:
				old_path.rename(new_path)
			except OSError as exc:
				log.warning(
					'_fix_dos_duplicate_extensions: could not rename %r → %r: %s',
					str(old_path), str(new_path), exc,
				)

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
extract *
exit
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.dim', delete=False) as f:
        f.write(script_content)
        script_path = f.name

    process_output = None
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
            # Rename any files whose names contain raw RISC OS Latin1 bytes to
            # their correct Unicode equivalents.  This must happen before
            # enumerate_extracted_files() or any tool receives these paths.
            normalize_extracted_filenames(output_dir)
            return {
                'success': True,
                'tool': 'DiscImageManager',
                'output_dir': str(output_dir),
                'file_count': file_count,
                'summary': f'Extracted {file_count} files from Acorn disc image',
                'process_output': process_output
            }

        # No files extracted.  Distinguish a valid-but-empty disc (DIM read
        # the image successfully) from a genuine failure (unrecognised format).
        # DIM prints "… image read OK." on success regardless of file count.
        stdout = ''
        if process_output:
            stdout = process_output.get('stdout', '')
        dim_read_ok = result.returncode == 0 and 'read OK' in stdout

        if dim_read_ok:
            return {
                'success': True,
                'tool': 'DiscImageManager',
                'output_dir': str(output_dir),
                'file_count': 0,
                'summary': 'Acorn disc image is valid but contains no files',
                'process_output': process_output
            }

        return {
            'success': False,
            'tool': 'DiscImageManager',
            'error': 'No files extracted - may not be Acorn format',
            'process_output': process_output
        }

    except Exception as e:
        # Ensure process output is logged even when extraction fails
        import traceback
        error_details = {
            'success': False,
            'tool': 'DiscImageManager',
            'error': f'Error extracting from disc image: {str(e)}',
            'exception_trace': traceback.format_exc()[:2000],
        }
        if process_output is not None:
            error_details['process_output'] = process_output
        return error_details

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

    try:
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
            # Some versions of 7z append the short 8.3 extension to the VFAT
            # long filename, producing double extensions like E002.ZIP.ZIP.
            # Fix these before any further processing.
            _fix_dos_duplicate_extensions(output_dir)
            # Normalise raw DOS/FAT byte sequences in filenames to Unicode.
            # CP850 (Western European DOS) is used as the default; see
            # _decode_dos_cp850() for rationale and limitations.
            normalize_extracted_filenames(output_dir, decoder=_decode_dos_cp850)
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

    except Exception as e:
        # Ensure process output is logged even when extraction fails
        import traceback
        error_details = {
            'success': False,
            'tool': '7z',
            'error': f'Error extracting from DOS image: {str(e)}',
            'exception_trace': traceback.format_exc()[:2000],
        }
        if 'process_output' in locals():
            error_details['process_output'] = process_output
        return error_details


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


def enumerate_extracted_files(output_dir: Path, acorn: bool = False) -> list[dict]:
    """
    Enumerate files in an extraction directory and return structured file list.

    Args:
        output_dir: Directory containing extracted files
        acorn: If True, parse Acorn filetype suffixes from filenames

    Returns:
        List of file dicts with path, size, and optional filetype/directory info
    """
    files = []

    for file_path in output_dir.rglob('*'):
        if not file_path.is_file():
            continue

        # Skip .inf metadata files (Acorn DIM extraction artifacts)
        if file_path.suffix == '.inf':
            continue

        rel_path = file_path.relative_to(output_dir)
        file_size = file_path.stat().st_size

        if acorn:
            # Parse Acorn filename to extract filetype
            true_name, filetype = parse_acorn_filename(file_path.name)

            # Reconstruct path with true filename (without filetype suffix)
            if filetype and len(rel_path.parts) > 1:
                display_path = str(Path(*rel_path.parts[:-1]) / true_name)
            elif filetype:
                display_path = true_name
            else:
                display_path = str(rel_path)

            file_entry = {
                'path': display_path,
                'size': file_size,
            }

            # Detect ADFS directories (typically 2KB/2048 bytes)
            is_directory = (
                file_size == 2048 or  # ADFS directory size
                filetype == 'ddc'     # RISC OS directory filetype
            )

            if is_directory:
                file_entry['is_directory'] = True

            # Store RISC OS filetype (hex string like '3fb') for archive detection
            if filetype:
                file_entry['risc_os_filetype'] = filetype
        else:
            display_path = str(rel_path)
            file_entry = {
                'path': display_path,
                'size': file_size,
            }

        files.append(file_entry)

    return files


def parse_acorn_filename(filename: str) -> tuple[str, str | None]:
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


def convert_fcfs_to_raw(input_path: Path, output_path: Path) -> dict:
    """
    Convert FCFS (Filecore) disk image to raw sector image using fcfs2raw.

    Args:
        input_path: Path to FCFS image file
        output_path: Path to output raw image file

    Returns:
        Result dict with success status
    """
    try:
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

    except Exception as e:
        # Ensure process output is logged even when conversion fails
        import traceback
        error_details = {
            'success': False,
            'tool': 'fcfs2raw',
            'error': f'Error converting FCFS image: {str(e)}',
            'exception_trace': traceback.format_exc()[:2000],
        }
        if 'process_output' in locals():
            error_details['process_output'] = process_output
        return error_details
