"""
Archive extraction tools.

Wraps external archive extraction tools (riscosarc, tbafs-extractor, etc.)
for use by the worker.
"""

import os
import subprocess
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, Any

from .base import run_tool_with_output
from ..compression import stream_to_file
from ..config import log, TOOL_TIMEOUT, MAX_DECOMPRESSED_BYTES
from ..utils.text import normalize_extracted_filenames


def _check_archive_paths(input_path: Path, fmt: str) -> None:
    """Reject the archive if any entry contains an unsafe path or link target.

    Uses Python's built-in zipfile / tarfile readers so the check happens before
    any external tool runs and cannot be bypassed by a crafted archive.

    Raises:
        ValueError: If a path-traversal entry, or an unsafe symlink/hardlink target,
                    is found.
    """
    if fmt == 'zip':
        with zipfile.ZipFile(input_path) as zf:
            for zi in zf.infolist():
                name = zi.filename
                parts = name.replace('\\', '/').split('/')
                if os.path.isabs(name) or '..' in parts:
                    raise ValueError(f'Unsafe path in ZIP archive: {name!r}')
                # Reject symlink entries.  Unix-originated ZIPs encode the file mode in
                # the upper 16 bits of external_attr; S_IFLNK == 0o120000.  A zero mode
                # means the ZIP was created on Windows and is not a symlink.
                mode = (zi.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) == 0o120000:
                    raise ValueError(f'Symlink entry in ZIP archive: {name!r}')
    elif fmt == 'tar':
        with tarfile.open(input_path) as tf:
            for m in tf.getmembers():
                parts = m.name.replace('\\', '/').split('/')
                if os.path.isabs(m.name) or '..' in parts:
                    raise ValueError(f'Unsafe path in TAR archive: {m.name!r}')
                # Also validate symlink and hardlink targets so that a relative link
                # like '../../etc/passwd' cannot escape the extraction directory.
                if m.issym() or m.islnk():
                    link_parts = m.linkname.replace('\\', '/').split('/')
                    if os.path.isabs(m.linkname) or '..' in link_parts:
                        raise ValueError(
                            f'Unsafe link target in TAR archive: {m.linkname!r}'
                        )


# Matches symlink-mode Unix permission strings (e.g. 'lrwxrwxrwx') or a
# standalone 'L' flag used by some 7z builds.
_7Z_SYMLINK_RE = re.compile(r'(?:^|\s)l[rwx-]{9}(?:\s|$)|(?:^|\s)L(?:\s|$)')


def _check_7z_paths(input_path: Path) -> None:
    """Reject the archive if 7z reports any absolute-path, traversal, or symlink entry.

    Uses '7z l -slt' technical listing.  The output contains an archive-level
    header block (introduced by '--' and ending at the first '----------' line)
    followed by one block per member.  The header's 'Path = ' line (the archive
    file's own path, which is an absolute path on the host) is skipped; only
    member blocks are validated.

    Raises:
        ValueError: If an unsafe path, traversal component, symlink entry, or
                    listing timeout is detected.
    """
    try:
        result = subprocess.run(
            ['7z', 'l', '-slt', str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ValueError('7z listing timed out — archive rejected')

    past_header = False
    path = None
    is_symlink = False

    def _validate(name: str, symlink: bool) -> None:
        if symlink:
            raise ValueError(f'Symlink entry in 7z archive: {name!r}')
        parts = name.replace('\\', '/').split('/')
        if os.path.isabs(name) or '..' in parts:
            raise ValueError(f'Unsafe path in 7z archive: {name!r}')

    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if line == '----------':
            if past_header:
                # End of a member block — validate collected path before reset.
                if path is not None:
                    _validate(path, is_symlink)
                path = None
                is_symlink = False
            else:
                # First '----------' ends the archive-level header; members follow.
                past_header = True
            continue

        if not past_header:
            continue

        if line.startswith('Path = '):
            path = line[7:]
        elif line.startswith('Attributes = '):
            # Detect symlinks via Unix permission string ('lrwxrwxrwx')
            # or a standalone 'L' flag used by some 7z builds.
            if _7Z_SYMLINK_RE.search(line[13:]):
                is_symlink = True

    # Validate the last member block (output may not end with '----------').
    if past_header and path is not None:
        _validate(path, is_symlink)


def _check_rar_paths(input_path: Path) -> None:
    """Reject the archive if unrar reports any absolute-path or traversal entry.

    Uses 'unrar vb' bare listing, which prints one path per line with no
    column formatting, making it straightforward to parse reliably.

    Raises:
        ValueError: If an unsafe path is found or the listing times out.
    """
    try:
        result = subprocess.run(
            ['unrar', 'vb', str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ValueError('unrar listing timed out — archive rejected')
    for name in result.stdout.decode('utf-8', errors='replace').splitlines():
        name = name.rstrip('/')
        if not name:
            continue
        parts = name.replace('\\', '/').split('/')
        if os.path.isabs(name) or '..' in parts:
            raise ValueError(f'Unsafe path in RAR archive: {name!r}')


def _assert_confined(output_dir: Path) -> None:
    """Raise if any extracted path has a realpath outside output_dir.

    Defence-in-depth for archive formats whose extractors cannot be pre-checked
    with Python's native readers.  If an extractor writes outside output_dir the
    damage is already done, but this ensures the analysis job fails loudly rather
    than silently completing with escaped files.

    Raises:
        ValueError: If any file has escaped the output directory.
    """
    real_base = str(output_dir.resolve())
    prefix = real_base + os.sep
    for entry in output_dir.rglob('*'):
        try:
            real = str(entry.resolve())
        except OSError:
            continue
        if real != real_base and not real.startswith(prefix):
            raise ValueError(f'Extracted file escaped output directory: {entry}')


def sanitize_extracted_tree(output_dir: Path) -> int:
    """Remove unsafe entries from an extracted archive tree.

    Removes symlinks and special files (device nodes, FIFOs, sockets) that
    could be used by a malicious archive to escape the extraction directory or
    access host resources.  Regular files and directories are left intact.

    Returns the number of entries removed.
    """
    removed = 0
    real_base = str(output_dir.resolve())
    # Walk bottom-up so we handle nested symlinks before their parents.
    for entry in sorted(output_dir.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_symlink():
                entry.unlink()
                removed += 1
            elif not entry.is_dir() and not entry.is_file():
                # Device node, FIFO, socket, etc.
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


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
    sanitize_extracted_tree(output_dir)
    try:
        _assert_confined(output_dir)
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'riscosarc'}

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
    sanitize_extracted_tree(output_dir)
    try:
        _assert_confined(output_dir)
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'tbafs-extractor'}

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

    try:
        _check_archive_paths(input_path, 'zip')
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'unzip'}

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
    sanitize_extracted_tree(output_dir)

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

    try:
        _check_archive_paths(input_path, 'tar')
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'tar'}

    # --no-absolute-filenames: explicitly reject entries with leading '/'
    # even though GNU tar strips them by default.
    cmd = ['tar', '--no-absolute-filenames'] + flags + ['-xf', str(input_path), '-C', str(output_dir)]

    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'tar failed with exit code {result.returncode}',
            'tool': 'tar',
            'process_output': output
        }

    normalize_extracted_filenames(output_dir)
    sanitize_extracted_tree(output_dir)

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

    try:
        _check_rar_paths(input_path)
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'unrar'}

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
    sanitize_extracted_tree(output_dir)

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

    try:
        _check_7z_paths(input_path)
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': '7z'}

    cmd = ['7z', 'x', '-y', f'-o{output_dir}', str(input_path)]
    result, output = run_tool_with_output(cmd)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'7z failed with exit code {result.returncode}',
            'tool': '7z',
            'process_output': output
        }

    sanitize_extracted_tree(output_dir)
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

    # stream_to_file() uses select() so the timeout is enforced mid-read,
    # not only after the loop exits.
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outcome = stream_to_file(proc, output_file, MAX_DECOMPRESSED_BYTES, TOOL_TIMEOUT)
        proc.stdout.close()
        proc.wait()

        if outcome == 'timeout':
            output_file.unlink(missing_ok=True)
            return {
                'success': False,
                'error': f'{compressor} timed out after {TOOL_TIMEOUT}s',
                'tool': compressor,
            }

        if outcome == 'size_exceeded':
            output_file.unlink(missing_ok=True)
            return {
                'success': False,
                'error': (
                    f'{compressor}: decompressed size exceeds '
                    f'{MAX_DECOMPRESSED_BYTES:,} byte limit'
                ),
                'tool': compressor,
            }

        stderr_text = proc.stderr.read().decode('utf-8', errors='replace')
        if proc.returncode != 0:
            return {
                'success': False,
                'error': f'{compressor} failed: {stderr_text}',
                'tool': compressor,
                'process_output': stderr_text,
            }

        return {
            'success': True,
            'tool': compressor,
            'file_count': 1,
            'summary': f'Decompressed file using {compressor}',
            'output_path': str(output_file),
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'{compressor} failed: {str(e)}',
            'tool': compressor,
        }

# vim: ts=4 sw=4 et
