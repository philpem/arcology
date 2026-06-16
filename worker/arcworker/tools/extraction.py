"""
File extraction tools.

Tools for listing and extracting files from disk images.
Supports:
- 7z - DOS/FAT, ISO, and many archive formats
- Disc Image Manager - Acorn DFS/ADFS filesystems
"""

import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from ..utils.text import normalize_extracted_filenames
from .base import exception_result, run_tool_with_output, tool_result
from .partition import read_fat_volume_label

_log = logging.getLogger(__name__)


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

    DIM produces INF sidecar files alongside extracted data files.  These are
    processed in-place: metadata is collected, files are renamed from
    DOS-encoded names to BBC originals, and the INF files are deleted.

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
        process_output['script'] = script_content

        # DIM can read DOS FAT12/16/32 images but produces double-extension
        # filenames (e.g. E002.ZIP → E002.ZIP.ZIP).  Detect this via the
        # 'report' output line "Container format: DOS ..." and reject it so
        # the caller falls through to extract_dos_7z.  The check is line-based
        # to avoid false positives from files named e.g. "The DOS FAT".
        stdout_text = process_output.get('stdout', '') if process_output else ''
        if any(line.strip().startswith('Container format: DOS')
               for line in stdout_text.splitlines()):
            shutil.rmtree(output_dir, ignore_errors=True)
            return tool_result(
                False, tool='DiscImageManager',
                error='DOS FAT filesystem — not an Acorn image',
                process_output=process_output,
            )

        # Pre-process extracted files before counting:
        # 1. Normalise RISC OS Latin-1 byte sequences in filenames to Unicode.
        # 2. Process INF sidecar files (extract metadata, rename DOS-encoded
        #    filenames to BBC originals, delete the .inf files).
        # Both must happen before enumerate_extracted_files() or any
        # downstream tool receives these paths.
        extracted_files = list(output_dir.rglob('*'))
        has_files = any(f.is_file() for f in extracted_files)

        inf_metadata = {}
        if has_files:
            normalize_extracted_filenames(output_dir)
            inf_metadata = process_inf_sidecars(output_dir)

        file_count = sum(1 for f in output_dir.rglob('*') if f.is_file())

        if file_count > 0:
            return tool_result(
                True, tool='DiscImageManager',
                process_output=process_output,
                output_dir=str(output_dir),
                file_count=file_count,
                inf_metadata=inf_metadata,
                summary=f'Extracted {file_count} files from Acorn disc image',
            )

        # No files extracted.  Distinguish a valid-but-empty disc (DIM read
        # the image successfully) from a genuine failure (unrecognised format).
        # DIM prints "… image read OK." on success regardless of file count.
        stdout = ''
        if process_output:
            stdout = process_output.get('stdout', '')
        dim_read_ok = result.returncode == 0 and 'read OK' in stdout

        if dim_read_ok:
            return tool_result(
                True, tool='DiscImageManager',
                process_output=process_output,
                output_dir=str(output_dir),
                file_count=0,
                summary='Acorn disc image is valid but contains no files',
            )

        return tool_result(
            False, tool='DiscImageManager',
            error='No files extracted - may not be Acorn format',
            process_output=process_output,
        )

    except Exception:
        # Ensure process output is logged even when extraction fails
        return exception_result(
            'DiscImageManager', 'Error extracting from disc image',
            process_output=process_output,
        )

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

    process_output = None
    try:
        cmd = [
            '7z', 'x',
            f'-o{output_dir}',
            '-y',  # Yes to all
            str(input_path)
        ]
        result, process_output = run_tool_with_output(cmd)

        # Remove the zero-byte phantom file that 7z creates from the FAT
        # ATTR_VOLUME_ID root-directory entry (the volume label).
        volume_label = read_fat_volume_label(input_path)
        if volume_label:
            phantom = output_dir / volume_label
            if phantom.is_file() and phantom.stat().st_size == 0:
                phantom.unlink()

        # Count extracted files
        extracted_files = list(output_dir.rglob('*'))
        file_count = sum(1 for f in extracted_files if f.is_file())

        if file_count > 0:
            # Normalise raw DOS/FAT byte sequences in filenames to Unicode.
            # CP850 (Western European DOS) is used as the default; see
            # _decode_dos_cp850() for rationale and limitations.
            normalize_extracted_filenames(output_dir, decoder=_decode_dos_cp850)
            return tool_result(
                True, tool='7z',
                process_output=process_output,
                output_dir=str(output_dir),
                file_count=file_count,
                summary=f'Extracted {file_count} files from DOS image',
            )

        return tool_result(
            False, tool='7z',
            error=(result.stderr.decode(errors='replace')[:1000]
                   if result.returncode != 0 else 'No files extracted'),
            process_output=process_output,
        )

    except Exception:
        # Ensure process output is logged even when extraction fails
        return exception_result(
            '7z', 'Error extracting from DOS image',
            process_output=process_output,
        )


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
    filetype_map: dict[str, str] | None = None,
    inf_metadata: dict[str, dict] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
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
        filetype_map: Optional mapping of lowercase file paths to RISC OS
            filetype hex strings (e.g. from the ISO ARCHIMEDES extension).
            Applied after suffix-based detection: files that already have a
            ``risc_os_filetype`` from their ``,xxx`` filename suffix are not
            overwritten.
        inf_metadata: Optional mapping of display paths to RISC OS metadata
            dicts (from :func:`process_inf_sidecars`).  Keys are
            ``load_address``, ``exec_address``, and optionally
            ``risc_os_filetype`` and ``attributes``.
        progress_callback: Optional ``callback(done, total)`` invoked after each
            file is hashed.  Hashing every extracted file is the dominant cost
            of this function, so this lets long extractions report live
            progress.  *total* comes from a cheap pre-count pass.

    Returns:
        List of file dicts with path, size, hashes, and optional
        filetype/directory info.
    """
    from ..utils.text import sanitize_path

    if acorn == 'auto':
        acorn = _has_acorn_filetypes(output_dir)

    files = []

    # Cheap pre-count for progress totals (a tree walk with no hashing — small
    # next to hashing every file).  Skipped entirely when no callback is given.
    total_files = (
        sum(1 for p in output_dir.rglob('*') if p.is_file())
        if progress_callback is not None else 0
    )
    done_files = 0

    for file_path in output_dir.rglob('*'):
        if not file_path.is_file():
            continue

        rel_path = file_path.relative_to(output_dir)
        stat = file_path.stat()
        file_size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(tzinfo=None)

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
                'modified_time': mtime.isoformat(),
            }

            # Store RISC OS filetype (hex string like '3fb') for archive detection
            if filetype:
                file_entry['risc_os_filetype'] = filetype
        else:
            file_entry = {
                'path': sanitize_path(str(rel_path)),
                'size': file_size,
                'modified_time': mtime.isoformat(),
            }

        if parent_file_id is not None:
            file_entry['parent_file_id'] = parent_file_id
        if extraction_depth is not None:
            file_entry['extraction_depth'] = extraction_depth

        # Apply ARCHIMEDES/ISO filetype map for files without a suffix-derived type
        if filetype_map and 'risc_os_filetype' not in file_entry:
            mapped_type = filetype_map.get(file_entry['path'].lower())
            if mapped_type:
                file_entry['risc_os_filetype'] = mapped_type

        # Apply INF sidecar metadata (load/exec addresses, filetype, attributes,
        # and RISC OS timestamp which overrides the filesystem mtime).
        if inf_metadata:
            inf_entry = inf_metadata.get(file_entry['path'])
            if inf_entry:
                if 'load_address' in inf_entry:
                    file_entry['load_address'] = inf_entry['load_address']
                if 'exec_address' in inf_entry:
                    file_entry['exec_address'] = inf_entry['exec_address']
                if 'attributes' in inf_entry:
                    file_entry['attributes'] = inf_entry['attributes']
                if 'risc_os_filetype' in inf_entry and 'risc_os_filetype' not in file_entry:
                    file_entry['risc_os_filetype'] = inf_entry['risc_os_filetype']
                if 'modified_time' in inf_entry:
                    file_entry['modified_time'] = inf_entry['modified_time']

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
        except OSError as e:
            # The record is still registered, just without hashes — log it so
            # a transient I/O error is distinguishable from "never hashed".
            _log.warning(f"Could not hash extracted file {file_path}: {e}")

        files.append(file_entry)

        if progress_callback is not None:
            done_files += 1
            progress_callback(done_files, total_files)

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


def _get_riscos_filetype(load_addr: int) -> str | None:
    """Extract a RISC OS filetype from a load address.

    When the top 12 bits of the load address (bits 31:20) are all 0xFFF,
    the file is date-stamped and bits 19:8 hold the 12-bit filetype.

    Returns the filetype as a lowercase 3-char hex string (e.g. 'fff'),
    or None if the load address is not in date-stamped format.
    """
    if (load_addr >> 20) == 0xFFF:
        filetype = (load_addr >> 8) & 0xFFF
        return f'{filetype:03x}'
    return None


def _riscos_timestamp_to_datetime(load_addr: int, exec_addr: int) -> datetime | None:
    """Decode a RISC OS 5-byte date-stamp to a naive UTC datetime.

    RISC OS date-stamped files store a 40-bit centisecond count since
    1900-01-01 00:00:00 UTC split across load and exec addresses when the
    top 12 bits of the load address are 0xFFF.  The low byte of the load
    address is the most-significant byte of the 5-byte timestamp; the full
    exec address provides the remaining four bytes.

    Returns None if the load address is not in date-stamped format, or if
    the resulting timestamp is out of range for a Python datetime.
    """
    if (load_addr >> 20) != 0xFFF:
        return None
    cs = ((load_addr & 0xFF) << 32) | exec_addr   # centiseconds since 1900-01-01
    # Difference between RISC OS epoch (1900-01-01) and Unix epoch (1970-01-01):
    # 70 years with 17 leap years = 25567 days = 2208988800 s = 220898880000 cs
    unix_cs = cs - 220898880000
    try:
        return datetime.fromtimestamp(unix_cs / 100, tz=timezone.utc).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


# BBC Micro <-> DOS/host filename character translation.
# Files stored on a host filesystem use the DOS-safe characters (right);
# the INF file records the original BBC characters (left).
_BBC_TO_DOS = {'#': '?', '.': '/', '$': '<', '^': '>', '&': '+', '@': '=', '%': ';'}
_DOS_TO_BBC = {v: k for k, v in _BBC_TO_DOS.items()}


def _translate_filename_dos_to_bbc(name: str) -> str:
    """Translate a DOS-encoded filename back to its BBC original."""
    return ''.join(_DOS_TO_BBC.get(c, c) for c in name)


def _translate_filename_bbc_to_dos(name: str) -> str:
    """Translate a BBC filename to its DOS/host filesystem equivalent."""
    return ''.join(_BBC_TO_DOS.get(c, c) for c in name)


def _parse_inf_line(line: str) -> dict | None:
    """Parse a single INF file line into a metadata dict.

    INF format::

        <filename> <load> <exec> [<length>] [<access>] [<extra info>]

    The filename may be quoted if it contains spaces.  Load, exec, and
    length are hex values.  Access can be letters or a hex number.

    Returns a dict with keys ``filename``, ``load_address``,
    ``exec_address``, and optionally ``risc_os_filetype`` and
    ``attributes``, or None if the line cannot be parsed.
    """
    line = line.strip()
    if not line:
        return None

    # Extract filename (possibly quoted)
    if line.startswith('"'):
        end_quote = line.find('"', 1)
        if end_quote == -1:
            return None
        filename = line[1:end_quote]
        rest = line[end_quote + 1:].split()
    else:
        parts = line.split()
        if len(parts) < 3:
            return None
        filename = parts[0]
        rest = parts[1:]

    if len(rest) < 2:
        return None

    try:
        load_int = int(rest[0], 16)
        exec_int = int(rest[1], 16)
    except (ValueError, IndexError):
        return None

    result = {
        'filename': filename,
        'load_address': f'{load_int:08x}',
        'exec_address': f'{exec_int:08x}',
    }

    # Derive filetype and timestamp from load address (date-stamped files only)
    filetype = _get_riscos_filetype(load_int)
    if filetype:
        result['risc_os_filetype'] = filetype
    ts = _riscos_timestamp_to_datetime(load_int, exec_int)
    if ts is not None:
        result['modified_time'] = ts.isoformat()

    # Access field (after optional length)
    # rest[2] could be length (hex) or access (letters/hex).
    # Length is always a hex number; access can be letters like "WR/r" or "L".
    # Heuristic: if rest[2] is a pure hex number AND there's a rest[3],
    # treat rest[2] as length and rest[3] as access.  Otherwise treat
    # rest[2] as access if it contains non-hex-digit letters.
    if len(rest) >= 3:
        field2 = rest[2]
        try:
            int(field2, 16)
            is_hex = True
        except ValueError:
            is_hex = False

        if is_hex and len(rest) >= 4:
            # rest[2] is length, rest[3] is access
            result['attributes'] = rest[3]
        elif not is_hex:
            # rest[2] is access (contains non-hex letters)
            result['attributes'] = field2
        # If rest[2] is hex and no rest[3], it's length with no access field

    return result


def process_inf_sidecars(output_dir: Path) -> dict[str, dict]:
    """Process RISC OS INF sidecar files in an extraction directory.

    For each ``.inf`` file found (case-insensitive), if a data file with
    the same name (minus the ``.inf`` extension) exists alongside it:

    1. Parse the INF to extract load/exec addresses, filetype, and attributes.
    2. Rename the data file from its DOS-encoded name to the BBC original
       (if they differ), preserving any ``,xxx`` filetype suffix.
    3. Delete the INF file.

    Must be called **before** :func:`enumerate_extracted_files` so that
    files have their final names when enumeration and hashing occur.

    Args:
        output_dir: Root directory containing extracted files and INF sidecars.

    Returns:
        Dict mapping relative paths (post-rename, relative to *output_dir*)
        to metadata dicts with ``load_address``, ``exec_address``, and
        optionally ``risc_os_filetype`` and ``attributes``.
    """
    metadata: dict[str, dict] = {}

    # Collect all INF files first (avoid modifying tree while iterating).
    inf_files = sorted(
        (p for p in output_dir.rglob('*') if p.is_file() and p.suffix.lower() == '.inf'),
        key=lambda p: len(p.parts),
        reverse=True,  # deepest first to avoid path invalidation
    )

    for inf_path in inf_files:
        # The data file has the same path minus the .inf extension.
        data_path = inf_path.with_suffix('')
        if not data_path.exists():
            # No matching data file (exact-case match only) — leave the
            # orphan INF in place rather than guessing.
            continue

        # Parse the INF
        try:
            try:
                content = inf_path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                content = inf_path.read_text(encoding='latin-1')
        except OSError as e:
            _log.warning(f"Could not read INF file {inf_path}: {e}")
            continue

        parsed = _parse_inf_line(content)
        if parsed is None:
            _log.warning(f"Could not parse INF file {inf_path}")
            continue

        bbc_filename = parsed.pop('filename')

        # Determine if the data file needs renaming.
        # The data file on disk may use DOS-translated characters.
        # The INF contains the original BBC filename.
        # Preserve any ,xxx filetype suffix that DIM may have added.
        current_name = data_path.name
        current_stem, current_suffix_type = parse_acorn_filename(current_name)

        # Translate the BBC name to DOS to see if it matches the on-disk name
        bbc_as_dos = _translate_filename_bbc_to_dos(bbc_filename)

        new_name = current_name
        if current_stem != bbc_filename and (current_stem == bbc_as_dos or current_name == bbc_as_dos):
            # The on-disk name is the DOS-encoded version; rename to BBC
            if current_suffix_type is not None:
                new_name = bbc_filename + ',' + current_suffix_type
            else:
                new_name = bbc_filename

        # Perform the rename if needed
        final_path = data_path
        if new_name != current_name:
            target = data_path.parent / new_name
            if target.exists() and target != data_path:
                _log.warning(
                    f"Cannot rename {data_path.name} -> {new_name}: "
                    f"target already exists"
                )
            else:
                try:
                    data_path.rename(target)
                    final_path = target
                except OSError as e:
                    _log.warning(f"Failed to rename {data_path} -> {target}: {e}")

        # Delete the INF file
        try:
            inf_path.unlink()
        except OSError as e:
            _log.warning(f"Could not delete INF file {inf_path}: {e}")

        # Store metadata keyed by relative path (post-rename).
        # Use the display path that enumerate_extracted_files will produce:
        # for Acorn files with ,xxx suffix, the display path strips the suffix.
        rel_path = final_path.relative_to(output_dir)
        display_name, _ = parse_acorn_filename(final_path.name)
        if len(rel_path.parts) > 1:
            display_rel = str(Path(*rel_path.parts[:-1]) / display_name)
        else:
            display_rel = display_name

        metadata[display_rel] = parsed

    return metadata


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
    process_output = None
    try:
        cmd = ['fcfs2raw', '-v', str(input_path), str(output_path)]
        result, process_output = run_tool_with_output(cmd)

        if result.returncode != 0:
            return tool_result(
                False, tool='fcfs2raw',
                error=f'fcfs2raw failed with exit code {result.returncode}',
                process_output=process_output,
            )

        return tool_result(
            True, tool='fcfs2raw',
            process_output=process_output,
            output_path=str(output_path),
            summary='FCFS image converted to raw sector format',
        )

    except Exception:
        # Ensure process output is logged even when conversion fails
        return exception_result(
            'fcfs2raw', 'Error converting FCFS image',
            process_output=process_output,
        )

# vim: ts=4 sw=4 et
