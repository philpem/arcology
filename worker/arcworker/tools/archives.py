"""
Archive extraction tools.

Wraps external archive extraction tools (riscosarc, tbafs-extractor, etc.)
for use by the worker.
"""

import os
import re
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from ..compression import stream_to_file
from ..config import MAX_DECOMPRESSED_BYTES, TOOL_TIMEOUT, log
from ..exceptions import JobCancelledException
from ..utils.text import decode_riscos_latin1, fix_riscos_c1_filenames, normalize_extracted_filenames
from .base import run_tool_with_output, tool_result


def _validate_entry_path(name: str, fmt: str) -> None:
    """Raise ValueError if *name* is absolute or contains '..' components."""
    parts = name.replace('\\', '/').split('/')
    if os.path.isabs(name) or '..' in parts:
        raise ValueError(f'Unsafe path in {fmt} archive: {name!r}')


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
                    _validate_entry_path(name, 'ZIP')
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
                _validate_entry_path(m.name, 'TAR')
                # Also validate symlink and hardlink targets so that a relative link
                # like '../../etc/passwd' cannot escape the extraction directory.
                if m.issym() or m.islnk():
                    _validate_entry_path(m.linkname, 'TAR link target')


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
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError('7z listing timed out — archive rejected') from e

    past_header = False
    path = None
    is_symlink = False

    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if line == '----------':
            if past_header:
                # End of a member block — validate collected path before reset.
                if path is not None:
                    if is_symlink:
                        raise ValueError(f'Symlink entry in 7z archive: {path!r}')
                    _validate_entry_path(path, '7z')
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
        if is_symlink:
            raise ValueError(f'Symlink entry in 7z archive: {path!r}')
        _validate_entry_path(path, '7z')


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
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError('unrar listing timed out — archive rejected') from e

    current_name: str | None = None
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if line.startswith('Name:'):
            # Validate the previous entry before moving on to the next.
            if current_name is not None:
                _validate_entry_path(current_name, 'RAR')
            current_name = line[5:].strip()
        elif line.startswith('Type:'):
            type_val = line[5:].strip().lower()
            # 'Symbolic link', 'Hard link', etc. — any link type is rejected.
            if 'link' in type_val:
                raise ValueError(f'Link entry in RAR archive: {current_name!r}')

    # Validate the final entry.
    if current_name is not None:
        _validate_entry_path(current_name, 'RAR')


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


def _archive_error(tool: str, message: str, process_output: dict | None = None) -> dict[str, Any]:
    """Build a standard failed archive-result payload."""
    return tool_result(False, tool=tool, error=message, process_output=process_output)


def _archive_success(
    tool: str,
    summary: str,
    file_count: int,
    process_output: dict | None = None,
    archive_comment: str | None = None,
) -> dict[str, Any]:
    """Build a standard successful archive-result payload."""
    extra: dict[str, Any] = {}
    if archive_comment:
        extra['archive_comment'] = archive_comment
    return tool_result(
        True,
        tool=tool,
        process_output=process_output,
        file_count=file_count,
        summary=summary,
        **extra,
    )


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
) -> dict[str, Any]:
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


def extract_riscosarc(input_path: Path, output_dir: Path) -> dict[str, Any]:
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


def extract_tbafs(input_path: Path, output_dir: Path) -> dict[str, Any]:
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
        cmd=['tbafs-extractor', 'x', str(input_path), '-o', str(output_dir)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from TBAFS archive',
        normalize_names=True,
        assert_confined=True,
    )


def read_zip_comment(zip_path: Path) -> str | None:
    """Return the ZIP archive-wide comment, or None if absent.

    The comment lives in the End-of-Central-Directory (EOCD) record at
    the end of a ZIP file.  ZIPs use Code Page 437 historically, so the
    bytes are decoded as cp437 (with replacement on invalid sequences).

    Uses raw struct parsing — Python's :mod:`zipfile` rejects RISC OS
    ZIPs with the Acorn 0x4341 extra-field as ``BadZipFile`` even when
    the EOCD itself is well-formed.

    Returns:
        The trimmed comment string, or None if the archive has no comment
        or could not be parsed.
    """
    try:
        with open(zip_path, 'rb') as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            search_start = max(0, file_size - 65557)
            fh.seek(search_start)
            tail = fh.read()

            eocd_pos = tail.rfind(b'PK\x05\x06')
            if eocd_pos < 0:
                return None
            eocd = tail[eocd_pos:]
            if len(eocd) < 22:
                return None

            comment_len = struct.unpack_from('<H', eocd, 20)[0]
            if comment_len == 0:
                return None

            comment_bytes = eocd[22:22 + comment_len]
            if len(comment_bytes) < comment_len:
                return None
    except OSError:
        return None

    text = comment_bytes.decode('cp437', errors='replace')
    text = text.rstrip('\x00').rstrip()
    return text or None


def extract_zip(input_path: Path, output_dir: Path) -> dict[str, Any]:
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
        return _archive_error('unzip', str(e))

    result = _run_extraction_command(
        tool='unzip',
        cmd=['unzip', '-F', '-q', str(input_path), '-d', str(output_dir)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from ZIP archive',
        assert_confined=True,
    )
    if result.get('success'):
        comment = read_zip_comment(input_path)
        if comment:
            result['archive_comment'] = comment
    return result





# Acorn/SparkFS extra-field header ID used in RISC OS ZIP archives.
# This two-byte little-endian value (0x4341 = "AC") marks extra-field
# blocks that carry RISC OS filetype, load/exec addresses and attributes.
_ACORN_EXTRA_FIELD_ID = b'\x41\x43'   # 0x4341 little-endian


def _read_zip_central_directory(zip_path: Path) -> bytes | None:
    """Return the raw central-directory bytes of a ZIP, or None on any error.

    Locates the End-of-Central-Directory record (within the trailing 64 KiB)
    and reads the central directory it points at.  Uses raw ``struct`` parsing
    — **not** Python's ``zipfile``, which rejects RISC OS zips' Acorn
    extra-field blocks with ``BadZipFile``.  Conservative: returns None rather
    than raising on a malformed/truncated archive.
    """
    try:
        with open(zip_path, 'rb') as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            fh.seek(max(0, file_size - 65557))
            tail = fh.read()

            eocd_pos = tail.rfind(b'PK\x05\x06')
            if eocd_pos < 0 or len(tail) - eocd_pos < 22:
                return None
            cd_size, cd_offset = struct.unpack_from('<II', tail, eocd_pos + 12)
            if cd_offset + cd_size > file_size:
                return None

            fh.seek(cd_offset)
            return fh.read(cd_size)
    except OSError:
        return None


def _iter_zip_central_dir(cd_data: bytes):
    """Yield ``(filename_bytes, flags, extra_bytes)`` for each central-dir entry."""
    pos = 0
    while pos + 46 <= len(cd_data):
        if cd_data[pos:pos + 4] != b'PK\x01\x02':
            break
        flags = struct.unpack_from('<H', cd_data, pos + 8)[0]
        fname_len, extra_len, comment_len = struct.unpack_from('<HHH', cd_data, pos + 28)
        name = cd_data[pos + 46:pos + 46 + fname_len]
        extra = cd_data[pos + 46 + fname_len:pos + 46 + fname_len + extra_len]
        yield name, flags, extra
        pos += 46 + fname_len + extra_len + comment_len


def has_riscos_zip_metadata(zip_path: Path) -> bool:
    """Detect whether a ZIP archive contains RISC OS metadata.

    Parses the ZIP central directory and checks each entry's extra-field
    chain for the Acorn/SparkFS header ID (0x4341).  Returns ``True`` as
    soon as one is found, ``False`` on any structural parse error.
    """
    cd_data = _read_zip_central_directory(zip_path)
    if cd_data is None:
        return False
    for _name, _flags, extra in _iter_zip_central_dir(cd_data):
        # Walk extra-field chain: each block is 2-byte ID + 2-byte size + data.
        epos = 0
        while epos + 4 <= len(extra):
            field_id = extra[epos:epos + 2]
            field_size = struct.unpack_from('<H', extra, epos + 2)[0]
            if field_id == _ACORN_EXTRA_FIELD_ID:
                return True
            epos += 4 + field_size
    return False


def list_zip_member_names(zip_path: Path) -> list[str] | None:
    """Return the ZIP's member filenames by parsing the central directory.

    Cheap: reads only the central directory, not member contents.  Returns
    ``None`` on any parse error so callers can fall back conservatively.
    """
    cd_data = _read_zip_central_directory(zip_path)
    if cd_data is None:
        return None
    names: list[str] = []
    for raw, flags, _extra in _iter_zip_central_dir(cd_data):
        encoding = 'utf-8' if (flags & 0x800) else 'cp437'
        names.append(raw.decode(encoding, errors='replace'))
    return names


def extract_zip_riscos(input_path: Path, output_dir: Path) -> dict[str, Any]:
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
        return _archive_error('unzip', str(e))

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
        archive_comment=read_zip_comment(input_path),
    )


def extract_tar(input_path: Path, output_dir: Path, archive_type: str = 'tar') -> dict[str, Any]:
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
        return _archive_error('tar', str(e))

    # GNU tar rejects entries with leading '/' by default.
    cmd = ['tar'] + flags + ['-xf', str(input_path), '-C', str(output_dir)]

    return _run_extraction_command(
        tool='tar',
        cmd=cmd,
        output_dir=output_dir,
        summary='Extracted {file_count} files from TAR archive',
        assert_confined=True,
    )


def extract_rar(input_path: Path, output_dir: Path) -> dict[str, Any]:
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
        return _archive_error('unrar', str(e))

    return _run_extraction_command(
        tool='unrar',
        cmd=['unrar', 'x', '-y', str(input_path), str(output_dir) + '/'],
        output_dir=output_dir,
        summary='Extracted {file_count} files from RAR archive',
        assert_confined=True,
    )


def extract_7z(input_path: Path, output_dir: Path) -> dict[str, Any]:
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
        return _archive_error('7z', str(e))

    return _run_extraction_command(
        tool='7z',
        cmd=['7z', 'x', '-y', f'-o{output_dir}', str(input_path)],
        output_dir=output_dir,
        summary='Extracted {file_count} files from 7z archive',
        normalize_names=False,
        assert_confined=True,
    )


def decompress_single_file(input_path: Path, output_file: Path, compressor: str) -> dict[str, Any]:
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
        return tool_result(False, tool=compressor, error=f'Unknown compressor: {compressor}')

    # stream_to_file() uses select() so the timeout is enforced mid-read,
    # not only after the loop exits.
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outcome, stderr_bytes = stream_to_file(
            proc, output_file, MAX_DECOMPRESSED_BYTES, TOOL_TIMEOUT
        )
        proc.wait()

        if outcome == 'cancelled':
            output_file.unlink(missing_ok=True)
            raise JobCancelledException(
                f'{compressor} decompression cancelled server-side'
            )

        if outcome == 'timeout':
            output_file.unlink(missing_ok=True)
            return tool_result(
                False, tool=compressor,
                error=f'{compressor} timed out after {TOOL_TIMEOUT}s',
            )

        if outcome == 'size_exceeded':
            output_file.unlink(missing_ok=True)
            return tool_result(
                False, tool=compressor,
                error=(
                    f'{compressor}: decompressed size exceeds '
                    f'{MAX_DECOMPRESSED_BYTES:,} byte limit'
                ),
            )

        stderr_text = stderr_bytes.decode('utf-8', errors='replace')
        if proc.returncode != 0:
            return tool_result(
                False, tool=compressor,
                error=f'{compressor} failed: {stderr_text}',
                process_output=stderr_text,
            )

        return tool_result(
            True, tool=compressor,
            file_count=1,
            summary=f'Decompressed file using {compressor}',
            output_path=str(output_file),
        )
    except JobCancelledException:
        raise  # propagate cancellation; don't mask it as a tool failure
    except Exception as e:
        return tool_result(False, tool=compressor, error=f'{compressor} failed: {str(e)}')


# ---------------------------------------------------------------------------
# X-Files archive extraction (pure-Python parser)
# ---------------------------------------------------------------------------

_XFILES_MAGIC = b'XFIL'
_XFILES_DIR_SIG = b'Andy'
_XFILES_FREE_MAGIC = 0x45455246   # "FREE" as a little-endian uint32
_XFILES_ATTR_ISDIR = 0x100
_XFILES_MAX_DEPTH = 200


def _xfiles_read_chunk(fh, chunk_table: list, chunk_num: int) -> bytes:
    """Return the payload bytes of chunk *chunk_num*.

    Raises ValueError for out-of-range, free, or truncated chunks.
    """
    if chunk_num >= len(chunk_table):
        raise ValueError(
            f'X-Files: chunk {chunk_num} out of range '
            f'(table has {len(chunk_table)} entries)'
        )
    offset, size, usage, _alloc = chunk_table[chunk_num]
    if usage == _XFILES_FREE_MAGIC:
        raise ValueError(f'X-Files: chunk {chunk_num} is marked free')
    if size == 0:
        return b''
    fh.seek(offset)
    data = fh.read(size)
    if len(data) != size:
        raise ValueError(
            f'X-Files: chunk {chunk_num}: expected {size} bytes, '
            f'got {len(data)}'
        )
    return data


def _xfiles_safe_name(name: str) -> None:
    """Raise ValueError if *name* is an unsafe path component."""
    if not name or name in ('.', '..') or '/' in name or '\x00' in name:
        raise ValueError(f'X-Files: unsafe filename: {name!r}')


def _xfiles_extract_dir(
    fh,
    chunk_table: list,
    chunk_num: int,
    out_dir,
    rel_parts: list,
    inf_metadata: dict,
    depth: int,
) -> None:
    """Recursively extract a directory chunk to *out_dir*.

    Args:
        fh:           open file handle (binary read)
        chunk_table:  list of (offset, size, usage, allocSize) tuples
        chunk_num:    chunk number of this directory
        out_dir:      root output directory (Path)
        rel_parts:    path components relative to *out_dir* for this directory
        inf_metadata: dict populated with per-file RISC OS metadata
        depth:        current recursion depth (guards against malformed images)
    """
    from .extraction import _get_riscos_filetype, _riscos_timestamp_to_datetime

    if depth > _XFILES_MAX_DEPTH:
        raise ValueError(
            f'X-Files: directory depth limit ({_XFILES_MAX_DEPTH}) exceeded'
        )

    data = _xfiles_read_chunk(fh, chunk_table, chunk_num)

    if len(data) < 16:
        raise ValueError(
            f'X-Files: directory chunk {chunk_num} too small ({len(data)} bytes)'
        )

    # Directory header: sig(4) + parent(4) + size(4) + used(4)
    if data[:4] != _XFILES_DIR_SIG:
        raise ValueError(
            f'X-Files: directory chunk {chunk_num}: '
            f'bad signature {data[:4]!r}'
        )
    _parent, hash_size, hash_used = struct.unpack_from('<III', data, 4)

    if hash_used > hash_size:
        raise ValueError(
            f'X-Files: directory chunk {chunk_num}: '
            f'used ({hash_used}) > capacity ({hash_size})'
        )

    hash_end = 16 + hash_size * 12
    if hash_end > len(data):
        raise ValueError(
            f'X-Files: directory chunk {chunk_num}: '
            f'hash table extends past chunk end'
        )

    for i in range(hash_used):
        ho = 16 + i * 12
        # xFiles_dirHash: nameStart[4] + entryPos(4) + node(4)
        entry_pos, node = struct.unpack_from('<II', data, ho + 4)

        # Parse the variable-length directory entry at entry_pos.
        ep = entry_pos
        if ep + 20 > len(data):
            raise ValueError(
                f'X-Files: directory chunk {chunk_num}: '
                f'entry {i} entryPos {ep:#x} out of range'
            )
        # xFiles_dirEntry: load + exec + size + attr + nameLen
        load, exec_, _fsize, attr, name_len = struct.unpack_from('<IIIII', data, ep)
        ep += 20

        if ep + name_len + 1 > len(data):
            raise ValueError(
                f'X-Files: directory chunk {chunk_num}: '
                f'entry {i} filename extends past chunk end'
            )
        name = decode_riscos_latin1(data[ep:ep + name_len])
        # Translate from RISC OS filenames to UNIX e.g. / <=> .
        name = name.replace('/', '.')
        _xfiles_safe_name(name)

        is_dir = bool(attr & _XFILES_ATTR_ISDIR)
        item_parts = rel_parts + [name]

        if is_dir:
            item_path = out_dir.joinpath(*item_parts)
            item_path.mkdir(parents=True, exist_ok=True)
            _xfiles_extract_dir(
                fh, chunk_table, node,
                out_dir, item_parts, inf_metadata, depth + 1,
            )
        else:
            item_path = out_dir.joinpath(*item_parts)
            item_path.parent.mkdir(parents=True, exist_ok=True)
            file_data = _xfiles_read_chunk(fh, chunk_table, node)
            item_path.write_bytes(file_data)

            # Build inf_metadata keyed by display path (matching what
            # enumerate_extracted_files will compute from the on-disk path).
            display_path = str(Path(*item_parts))
            meta: dict = {
                'load_address': f'{load:08x}',
                'exec_address': f'{exec_:08x}',
                'attributes': f'{attr & 0xFF:02x}',
            }
            filetype = _get_riscos_filetype(load)
            if filetype:
                meta['risc_os_filetype'] = filetype
                ts = _riscos_timestamp_to_datetime(load, exec_)
                if ts:
                    meta['modified_time'] = ts.isoformat()
            inf_metadata[display_path] = meta


def extract_xfiles(input_path: Path, output_dir: Path) -> dict[str, Any]:
    """Extract an X-Files archive (RISC OS filetype &B23).

    X-Files is a chunk-based archive format by Andy Armstrong that stores files
    with long filenames and full RISC OS metadata (load/exec addresses,
    attributes).  The format is a mini filesystem: a fixed header locates a
    chunk table, which in turn locates directory and file data chunks.

    Returns a result dict matching the standard archive extractor convention,
    with an additional ``inf_metadata`` key populated with per-file RISC OS
    metadata suitable for passing to :func:`.extraction.enumerate_extracted_files`.
    """
    TOOL = 'xfiles-python'

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open(input_path, 'rb') as fh:
            # ── Validate 52-byte file header ─────────────────────────────────
            header = fh.read(52)
            if len(header) < 52:
                return _archive_error(
                    TOOL, 'X-Files: file too small to contain a valid header'
                )
            if header[:4] != _XFILES_MAGIC:
                return _archive_error(
                    TOOL,
                    f'X-Files: invalid magic {header[:4]!r} (expected b"XFIL")',
                )
            _hdr_size, struct_ver, _dir_ver = struct.unpack_from('<III', header, 4)
            if struct_ver != 1:
                return _archive_error(
                    TOOL,
                    f'X-Files: unsupported structure version {struct_ver} (expected 1)',
                )

            # Chunk table descriptor lives at header offset 0x10.
            ct_offset, ct_size, _ct_usage, _ct_alloc = struct.unpack_from(
                '<IIII', header, 0x10
            )
            root_chunk = struct.unpack_from('<I', header, 0x20)[0]

            if ct_size % 16 != 0:
                return _archive_error(
                    TOOL,
                    f'X-Files: chunk table size {ct_size} is not a multiple of 16',
                )

            # ── Read the chunk table ──────────────────────────────────────────
            fh.seek(ct_offset)
            ct_data = fh.read(ct_size)
            if len(ct_data) != ct_size:
                return _archive_error(
                    TOOL,
                    f'X-Files: chunk table truncated '
                    f'(expected {ct_size}, got {len(ct_data)})',
                )

            num_chunks = ct_size // 16
            chunk_table = [
                struct.unpack_from('<IIII', ct_data, i * 16)
                for i in range(num_chunks)
            ]

            if root_chunk >= num_chunks:
                return _archive_error(
                    TOOL,
                    f'X-Files: rootChunk {root_chunk} out of range '
                    f'(table has {num_chunks} entries)',
                )

            # ── Recursively extract from the root directory ───────────────────
            inf_metadata: dict[str, dict] = {}
            _xfiles_extract_dir(
                fh, chunk_table, root_chunk,
                output_dir, [], inf_metadata, 0,
            )

    except ValueError as exc:
        return _archive_error(TOOL, str(exc))
    except OSError as exc:
        return _archive_error(TOOL, f'X-Files: I/O error: {exc}')

    finalize_error = _finalize_extraction(
        output_dir,
        normalize_names=True,
        assert_confined=True,
    )
    if finalize_error:
        return _archive_error(TOOL, finalize_error)

    file_count = _count_extracted_files(output_dir)
    result = _archive_success(
        TOOL,
        f'Extracted {file_count} files from X-Files archive',
        file_count,
    )
    result['inf_metadata'] = inf_metadata
    return result


# vim: ts=4 sw=4 et
