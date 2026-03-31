"""
Archive extraction tools.

Wraps external archive extraction tools (riscosarc, tbafs-extractor, etc.)
for use by the worker.
"""

import os
import struct
import subprocess
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, Any

from .base import run_tool_with_output
from ..compression import stream_to_file
from ..config import log, TOOL_TIMEOUT, MAX_DECOMPRESSED_BYTES
from ..utils.text import normalize_extracted_filenames, fix_riscos_c1_filenames


def _check_archive_paths(input_path: Path, fmt: str) -> None:
    """Reject the archive if any entry contains an unsafe path or link target.

    Uses Python's built-in zipfile / tarfile readers so the check happens before
    any external tool runs and cannot be bypassed by a crafted archive.

    Raises:
        ValueError: If a path-traversal entry, or an unsafe symlink/hardlink target,
                    is found.
    """
    if fmt == 'zip':
        try:
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
        except zipfile.BadZipFile:
            # Python's zipfile rejects some valid vendor-specific extra fields,
            # most commonly 0x4341 (Acorn/SparkFS RISC OS metadata), raising
            # BadZipFile even though the archive is structurally sound and
            # extractable by unzip/7z.  Fall back to 7z for path validation so
            # these legitimate archives are not silently rejected.
            _check_7z_paths(input_path)
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
    """Reject the archive if unrar reports any absolute-path, traversal, or link entry.

    Uses 'unrar lt' technical listing, which provides 'Name:' and 'Type:' fields
    per member.  This allows detection of symbolic and hard links (both of which
    carry 'link' in their Type value) before any extractor runs.

    Raises:
        ValueError: If an unsafe path, traversal component, or link entry is
                    found, or if the listing times out.
    """
    try:
        result = subprocess.run(
            ['unrar', 'lt', str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ValueError('unrar listing timed out — archive rejected')

    current_name: str | None = None
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if line.startswith('Name:'):
            # Validate the previous entry before moving on to the next.
            if current_name is not None:
                parts = current_name.replace('\\', '/').split('/')
                if os.path.isabs(current_name) or '..' in parts:
                    raise ValueError(f'Unsafe path in RAR archive: {current_name!r}')
            current_name = line[5:].strip()
        elif line.startswith('Type:'):
            type_val = line[5:].strip().lower()
            # 'Symbolic link', 'Hard link', etc. — any link type is rejected.
            if 'link' in type_val:
                raise ValueError(f'Link entry in RAR archive: {current_name!r}')

    # Validate the final entry.
    if current_name is not None:
        parts = current_name.replace('\\', '/').split('/')
        if os.path.isabs(current_name) or '..' in parts:
            raise ValueError(f'Unsafe path in RAR archive: {current_name!r}')


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


def _archive_error(tool: str, message: str, process_output: str | None = None) -> Dict[str, Any]:
    """Build a standard failed archive-result payload."""
    result = {
        'success': False,
        'error': message,
        'tool': tool,
    }
    if process_output is not None:
        result['process_output'] = process_output
    return result


def _archive_success(
    tool: str,
    summary: str,
    file_count: int,
    process_output: str | None = None,
) -> Dict[str, Any]:
    """Build a standard successful archive-result payload."""
    result = {
        'success': True,
        'tool': tool,
        'file_count': file_count,
        'summary': summary,
    }
    if process_output is not None:
        result['process_output'] = process_output
    return result


def _count_extracted_files(output_dir: Path) -> int:
    """Return the number of regular files extracted under output_dir."""
    return sum(1 for entry in output_dir.rglob('*') if entry.is_file())


def _finalize_extraction(
    output_dir: Path,
    *,
    normalize_names: bool = True,
    assert_confined: bool = False,
) -> str | None:
    """Run shared post-extraction cleanup and validation.

    Returns:
        None if finalization succeeded, otherwise an error string.
    """
    if normalize_names:
        normalize_extracted_filenames(output_dir)
    sanitize_extracted_tree(output_dir)
    if assert_confined:
        try:
            _assert_confined(output_dir)
        except ValueError as exc:
            return str(exc)
    return None


def _run_extraction_command(
    *,
    tool: str,
    cmd: list[str],
    output_dir: Path,
    summary: str,
    cwd: str | None = None,
    normalize_names: bool = True,
    assert_confined: bool = False,
) -> Dict[str, Any]:
    """Run an extractor command and apply the common post-processing flow."""
    result, output = run_tool_with_output(cmd, cwd=cwd)

    if result.returncode != 0:
        return _archive_error(
            tool, f'{tool} failed with exit code {result.returncode}', output
        )

    finalize_error = _finalize_extraction(
        output_dir,
        normalize_names=normalize_names,
        assert_confined=assert_confined,
    )
    if finalize_error:
        return _archive_error(tool, finalize_error)

    file_count = _count_extracted_files(output_dir)
    return _archive_success(
        tool,
        summary.format(file_count=file_count),
        file_count,
        output,
    )


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
        return _archive_error(
            'riscosarc',
            f'riscosarc failed with exit code {result.returncode}',
            output,
        )

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
    finalize_error = _finalize_extraction(
        output_dir,
        normalize_names=True,
        assert_confined=True,
    )
    if finalize_error:
        return _archive_error('riscosarc', finalize_error)

    file_count = _count_extracted_files(output_dir)
    return _archive_success(
        'riscosarc',
        f'Extracted {file_count} files using riscosarc',
        file_count,
        output,
    )


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

    return _run_extraction_command(
        tool='tbafs-extractor',
        cmd=['tbafs-extractor', str(input_path), str(output_dir)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from TBAFS archive',
        normalize_names=True,
        assert_confined=True,
    )


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

    return _run_extraction_command(
        tool='unzip',
        cmd=['unzip', '-F', '-q', str(input_path), '-d', str(output_dir)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from ZIP archive',
    )





# Acorn/SparkFS extra-field header ID used in RISC OS ZIP archives.
# This two-byte little-endian value (0x4341 = "AC") marks extra-field
# blocks that carry RISC OS filetype, load/exec addresses and attributes.
_ACORN_EXTRA_FIELD_ID = b'\x41\x43'   # 0x4341 little-endian


def has_riscos_zip_metadata(zip_path: Path) -> bool:
    """Detect whether a ZIP archive contains RISC OS metadata.

    Parses the ZIP central directory and checks each entry's extra-field
    chain for the Acorn/SparkFS header ID (0x4341).  Returns ``True`` as
    soon as one is found.

    Uses raw binary parsing with ``struct`` — **not** Python's ``zipfile``
    module, which rejects the Acorn extra-field blocks with ``BadZipFile``.

    This function is intentionally conservative: it returns ``False`` on
    any structural parse error rather than raising.
    """
    try:
        with open(zip_path, 'rb') as fh:
            # ── Locate End-of-Central-Directory (EOCD) record ──────
            # EOCD is at most 22 + 65535 bytes from the end of the file
            # (22-byte fixed record + up to 65535-byte comment).
            fh.seek(0, 2)
            file_size = fh.tell()
            search_start = max(0, file_size - 65557)
            fh.seek(search_start)
            tail = fh.read()

            eocd_pos = tail.rfind(b'PK\x05\x06')
            if eocd_pos < 0:
                return False

            eocd = tail[eocd_pos:]
            if len(eocd) < 22:
                return False

            cd_size, cd_offset = struct.unpack_from('<II', eocd, 12)
            if cd_offset + cd_size > file_size:
                return False

            # ── Read and walk the central directory ────────────────
            fh.seek(cd_offset)
            cd_data = fh.read(cd_size)

            pos = 0
            while pos + 46 <= len(cd_data):
                if cd_data[pos:pos + 4] != b'PK\x01\x02':
                    break
                fname_len, extra_len, comment_len = struct.unpack_from(
                    '<HHH', cd_data, pos + 28
                )
                extra_start = pos + 46 + fname_len
                extra_end = extra_start + extra_len

                # Walk extra-field chain: each block is 2-byte ID + 2-byte size + data.
                epos = extra_start
                while epos + 4 <= extra_end:
                    field_id = cd_data[epos:epos + 2]
                    field_size = struct.unpack_from('<H', cd_data, epos + 2)[0]
                    if field_id == _ACORN_EXTRA_FIELD_ID:
                        return True
                    epos += 4 + field_size

                pos = extra_end + comment_len

    except OSError:
        pass
    return False


def extract_zip_riscos(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """Extract a RISC OS ZIP archive with correct Latin-1 filename encoding.

    RISC OS archives store filenames in RISC OS Latin-1.  The hard space used
    as a word separator in Acorn filenames is byte 0xA0.  Without special
    handling, unzip decodes filenames as CP437, turning 0xA0 into 'á'
    (U+00E1).

    ``unzip -O iso-8859-1`` instructs unzip to interpret non-UTF-8 ZIP
    filenames as ISO 8859-1 (Latin-1) instead.  RISC OS Latin-1 is identical
    to ISO 8859-1 for bytes 0xA0–0xFF, so 0xA0 correctly becomes U+00A0
    (NBSP).  Bytes 0x80–0x9F are ISO 8859-1 C1 control codes; a second pass
    with :func:`fix_riscos_c1_filenames` remaps those to their RISC OS
    printable-character equivalents.

    Requires the container locale to be UTF-8 (``LANG=C.UTF-8`` or
    equivalent) so that unzip's internal iconv conversion from ISO 8859-1 can
    target UTF-8 rather than ASCII.

    Args:
        input_path: Path to ZIP file
        output_dir: Directory to extract to

    Returns:
        Result dict with success status
    """
    log.info('extract_zip_riscos: extracting %s → %s', input_path, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        _check_archive_paths(input_path, 'zip')
    except ValueError as e:
        return {'success': False, 'error': str(e), 'tool': 'unzip'}

    result, output = run_tool_with_output(
        ['unzip', '-F', '-O', 'iso-8859-1', '-q', str(input_path), '-d', str(output_dir)]
    )

    if result.returncode != 0:
        return _archive_error(
            'unzip', f'unzip failed with exit code {result.returncode}', output
        )

    # Remap ISO 8859-1 C1 control codes (U+0080–U+009F) that unzip decoded
    # from bytes 0x80–0x9F to their RISC OS Latin-1 printable equivalents.
    fix_riscos_c1_filenames(output_dir)

    finalize_error = _finalize_extraction(
        output_dir,
        normalize_names=True,
        assert_confined=True,
    )
    if finalize_error:
        return _archive_error('unzip', finalize_error)

    file_count = _count_extracted_files(output_dir)
    return _archive_success(
        'unzip',
        f'Extracted {file_count} files from RISC OS ZIP archive',
        file_count,
        output,
    )


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

    return _run_extraction_command(
        tool='tar',
        cmd=cmd,
        output_dir=output_dir,
        summary='Extracted {file_count} files from TAR archive',
    )


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

    return _run_extraction_command(
        tool='unrar',
        cmd=['unrar', 'x', '-y', str(input_path), str(output_dir) + '/'],
        output_dir=output_dir,
        summary='Extracted {file_count} files from RAR archive',
        assert_confined=True,
    )


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

    return _run_extraction_command(
        tool='7z',
        cmd=['7z', 'x', '-y', f'-o{output_dir}', str(input_path)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from 7z archive',
        normalize_names=False,
    )


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
        outcome, stderr_bytes = stream_to_file(
            proc, output_file, MAX_DECOMPRESSED_BYTES, TOOL_TIMEOUT
        )
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

        stderr_text = stderr_bytes.decode('utf-8', errors='replace')
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
