"""
Archive extraction tools.

Wraps external archive extraction tools (riscosarc, tbafs-extractor, etc.)
for use by the worker.
"""

import subprocess
import re
from pathlib import Path
from typing import Dict, Any

from .base import run_tool_with_output
from ..utils.text import normalize_extracted_filenames


def extract_riscosarc(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """
    Extract archive using riscosarc.

    Supports: ArcFS, CFS, PackDir, Squash, Spark.

    Args:
        input_path: Path to archive file
        output_dir: Directory to extract to

    Returns:
        Result dict with success status, file count, tool name, etc.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # input_path is guaranteed to be a clean Unicode path: the disc image
    # extraction pipeline calls normalize_extracted_filenames() before any
    # archive job is queued, so the .arc file on disk already has a UTF-8 name.
    cmd = ['riscosarc', '-x', '-F', str(input_path)]
    result, output = run_tool_with_output(cmd, cwd=str(output_dir))

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'riscosarc failed with exit code {result.returncode}',
            'tool': 'riscosarc',
            'process_output': output
        }

    # Scan for files with double extensions and rename them.
    # This generally happens with Squash, where riscosarc produces a file
    # like "MyFile,FCA,BBC" (input filetype + original filetype).
    # We strip the input filetype, leaving "MyFile,BBC".
    # IMPORTANT: use f.parent to keep the file in output_dir, not CWD.
    de_re = re.compile(r'(.*),([0-9A-Fa-f]+),([0-9A-Fa-f]+)')
    for f in output_dir.rglob('*'):
        if f.is_file():
            m = de_re.match(f.name)
            if m is not None:
                f.rename(f.parent / f'{m.group(1)},{m.group(3)}')

    # Normalise any RISC OS Latin1 byte sequences in extracted filenames.
    normalize_extracted_filenames(output_dir)

    # Count extracted files
    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'riscosarc',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files using riscosarc',
        'process_output': output
    }


def extract_tbafs(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """
    Extract TBAFS archive.

    Args:
        input_path: Path to TBAFS archive
        output_dir: Directory to extract to

    Returns:
        Result dict with success status
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['tbafs-extractor', str(input_path), str(output_dir)]
    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'tbafs-extractor failed with exit code {result.returncode}',
            'tool': 'tbafs-extractor',
            'process_output': output
        }

    normalize_extracted_filenames(output_dir)

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'tbafs-extractor',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files from TBAFS archive',
        'process_output': output
    }


def extract_zip(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """
    Extract ZIP archive.

    Args:
        input_path: Path to ZIP file
        output_dir: Directory to extract to

    Returns:
        Result dict with success status
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['unzip', '-F', '-q', str(input_path), '-d', str(output_dir)]
    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'unzip failed with exit code {result.returncode}',
            'tool': 'unzip',
            'process_output': output
        }

    # Normalise any raw Latin-1 byte sequences in extracted filenames
    # (e.g. RISC OS filenames stored in the ZIP with non-UTF-8 bytes).
    normalize_extracted_filenames(output_dir)

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'unzip',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files from ZIP archive',
        'process_output': output
    }


def extract_tar(input_path: Path, output_dir: Path, archive_type: str = 'tar') -> Dict[str, Any]:
    """
    Extract TAR archive (optionally compressed).

    Args:
        input_path: Path to TAR file
        output_dir: Directory to extract to
        archive_type: Type of TAR archive ('tar', 'tar_gz', 'tar_bz2', 'tar_xz')

    Returns:
        Result dict with success status
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine compression flag
    compression_flags = {
        'tar': [],
        'tar_gz': ['-z'],
        'tar_bz2': ['-j'],
        'tar_xz': ['-J']
    }

    flags = compression_flags.get(archive_type, [])
    cmd = ['tar'] + flags + ['-xf', str(input_path), '-C', str(output_dir)]

    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'tar failed with exit code {result.returncode}',
            'tool': 'tar',
            'process_output': output
        }

    normalize_extracted_filenames(output_dir)

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'tar',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files from TAR archive',
        'process_output': output
    }


def extract_rar(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """
    Extract RAR archive.

    Args:
        input_path: Path to RAR file
        output_dir: Directory to extract to

    Returns:
        Result dict with success status
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['unrar', 'x', '-y', str(input_path), str(output_dir) + '/']
    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'unrar failed with exit code {result.returncode}',
            'tool': 'unrar',
            'process_output': output
        }

    normalize_extracted_filenames(output_dir)

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'unrar',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files from RAR archive',
        'process_output': output
    }


def extract_7z(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """
    Extract 7-Zip archive.

    Args:
        input_path: Path to 7z file
        output_dir: Directory to extract to

    Returns:
        Result dict with success status
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['7z', 'x', '-y', f'-o{output_dir}', str(input_path)]
    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'7z failed with exit code {result.returncode}',
            'tool': '7z',
            'process_output': output
        }

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': '7z',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files from 7z archive',
        'process_output': output
    }


def decompress_single_file(input_path: Path, output_file: Path, compressor: str) -> Dict[str, Any]:
    """
    Decompress a single-file compressor (gzip, bzip2, xz, zstd).

    Args:
        input_path: Path to compressed file
        output_file: Path to decompressed output file
        compressor: Compressor type ('gzip', 'bzip2', 'xz', 'zstd')

    Returns:
        Result dict with success status
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    commands = {
        'gzip': ['gzip', '-d', '-c', str(input_path)],
        'bzip2': ['bzip2', '-d', '-c', str(input_path)],
        'xz': ['xz', '-d', '-c', str(input_path)],
        'zstd': ['zstd', '-d', '-c', str(input_path)]
    }

    cmd = commands.get(compressor)
    if not cmd:
        return {
            'success': False,
            'error': f'Unknown compressor: {compressor}',
            'tool': compressor
        }

    # Run decompression and redirect output to file
    try:
        with open(output_file, 'wb') as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            return {
                'success': False,
                'error': f'{compressor} failed: {result.stderr}',
                'tool': compressor,
                'process_output': result.stderr
            }

        return {
            'success': True,
            'tool': compressor,
            'file_count': 1,
            'summary': f'Decompressed file using {compressor}',
            'output_path': str(output_file)
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'{compressor} failed: {str(e)}',
            'tool': compressor
        }

# vim: ts=4 sw=4 et
