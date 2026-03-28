"""
File extraction tools.

Tools for listing and extracting files from disk images.
Supports:
- 7z - DOS/FAT, ISO, and many archive formats
- Disc Image Manager - Acorn DFS/ADFS filesystems
"""

import hashlib
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

        # DIM can read DOS FAT12/16/32 images but produces double-extension
        # filenames (e.g. E002.ZIP → E002.ZIP.ZIP).  Detect this via the
        # 'report' output line "Container format: DOS ..." and reject it so
        # the caller falls through to extract_dos_7z.  The check is line-based
        # to avoid false positives from files named e.g. "The DOS FAT".
        stdout_text = process_output.get('stdout', '') if process_output else ''
        if any(line.strip().startswith('Container format: DOS')
               for line in stdout_text.splitlines()):
            shutil.rmtree(output_dir, ignore_errors=True)
            return {
                'success': False,
                'tool': 'DiscImageManager',
                'error': 'DOS FAT filesystem — not an Acorn image',
                'process_output': process_output,
            }

        # Count extracted files
        extracted_files = list(output_dir.rglob('*'))
        file_count = sum(1 for f in extracted_files if f.is_file() and f.suffix.lower() != '.inf')

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


def _has_acorn_filetypes(output_dir: Path) -> bool:
    """Auto-detect whether extracted files use Acorn ``,xxx`` filetype suffixes.

    Scans up to 50 files; if any have a ``name,hex`` suffix that parses as a
    valid RISC OS filetype, the directory is treated as Acorn.
    """
    count = 0
    for file_path in output_dir.rglob('*'):
        if not file_path.is_file():
            continue
        _, filetype = parse_acorn_filename(file_path.name)
        if filetype is not None:
            return True
        count += 1
        if count >= 50:
            break
    return False


def enumerate_extracted_files(
    output_dir: Path,
    acorn: bool | str = False,
    parent_file_id: int | None = None,
    extraction_depth: int | None = None,
) -> list[dict]:
    """
    Enumerate files in an extraction directory and return structured file list.

    This is the single implementation used by FILE_EXTRACTION (disc images),
    ARCHIVE_EXTRACT (nested archives), and top-level archive extraction.

    Args:
        output_dir: Directory containing extracted files
        acorn: ``True`` to always parse Acorn filetype suffixes, ``False``
            to never parse them, or ``'auto'`` to auto-detect by scanning
            filenames for ``,xxx`` hex suffixes.
        parent_file_id: If set, included in every file entry (used by
            nested-archive extraction to record lineage).
        extraction_depth: If set, included in every file entry.

    Returns:
        List of file dicts with path, size, hashes, and optional
        filetype/directory info.
    """
    from ..utils.text import sanitize_path

    if acorn == 'auto':
        acorn = _has_acorn_filetypes(output_dir)

    files = []

    for file_path in output_dir.rglob('*'):
        if not file_path.is_file():
            continue

        # Skip .inf metadata files (Acorn DIM extraction artifacts)
        if acorn and file_path.suffix.lower() == '.inf':
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
                'path': sanitize_path(display_path),
                'size': file_size,
            }

            # Store RISC OS filetype (hex string like '3fb') for archive detection
            if filetype:
                file_entry['risc_os_filetype'] = filetype
        else:
            file_entry = {
                'path': sanitize_path(str(rel_path)),
                'size': file_size,
            }

        if parent_file_id is not None:
            file_entry['parent_file_id'] = parent_file_id
        if extraction_depth is not None:
            file_entry['extraction_depth'] = extraction_depth

        # Compute hashes so they can be stored in the DB at registration time.
        # This avoids needing to locate the file on disk later (e.g. for hash
        # database population) and correctly handles Acorn display-path vs
        # on-disk `,xxx` suffix mismatches.
        try:
            md5_h = hashlib.md5()
            sha1_h = hashlib.sha1()
            sha256_h = hashlib.sha256()
            with open(file_path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(65536), b''):
                    md5_h.update(chunk)
                    sha1_h.update(chunk)
                    sha256_h.update(chunk)
            file_entry['md5'] = md5_h.hexdigest()
            file_entry['sha1'] = sha1_h.hexdigest()
            file_entry['sha256'] = sha256_h.hexdigest()
        except OSError:
            pass

        files.append(file_entry)

    # Detect empty directories (no files underneath) and record them explicitly.
    # Without this, empty directories are invisible in the file listing because
    # the UI infers subdirectory buttons only from file paths.
    for dir_path in output_dir.rglob('*'):
        if not dir_path.is_dir():
            continue
        if any(True for f in dir_path.rglob('*') if f.is_file()):
            continue  # non-empty: already represented via file paths
        rel_path = dir_path.relative_to(output_dir)
        dir_entry: dict = {
            'path': sanitize_path(str(rel_path)),
            'is_directory': True,
        }
        if parent_file_id is not None:
            dir_entry['parent_file_id'] = parent_file_id
        if extraction_depth is not None:
            dir_entry['extraction_depth'] = extraction_depth
        files.append(dir_entry)

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

# vim: ts=4 sw=4 et
