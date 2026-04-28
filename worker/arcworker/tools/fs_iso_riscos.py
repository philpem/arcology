"""
RISC OS filetype extraction from ISO 9660 disc images.

Parses the ARCHIMEDES ISO 9660 extension (a 32-byte block at the start of
each directory record's System Use area) to extract RISC OS load/exec
addresses and derive filetypes.  Also scans for Rock Ridge NM entries that
may contain filenames with the Acorn ',xxx' filetype suffix.

No external dependencies — uses stdlib struct only.

Reference:
  http://justsolve.archiveteam.org/wiki/ARCHIMEDES_ISO_9660_extension
  Acorn Application Note 273: CD ROM Drives and their Handling under RISC OS
"""

import logging
import struct
from pathlib import Path
from .extraction import parse_acorn_filename

log = logging.getLogger(__name__)

# ISO 9660 sector size in bytes
_SECTOR = 2048

# Signature for the ARCHIMEDES System Use block (10 bytes, raw at start of SU area)
_ARCHIMEDES_SIG = b'ARCHIMEDES'

# Minimum size of a valid ARCHIMEDES block: 10-byte sig + 4 load + 4 exec + 4 attrs = 22 bytes
_ARCHIMEDES_MIN = 22

# Total size of the ARCHIMEDES block (sig + load + exec + attrs + 10 reserved = 32 bytes).
# SUSP scanning starts at this offset when the block is present.
_ARCHIMEDES_SIZE = 32


def _read_sector(f, lba: int) -> bytes:
    """Read one 2048-byte ISO 9660 sector at the given Logical Block Address."""
    f.seek(lba * _SECTOR)
    return f.read(_SECTOR)


def _get_riscos_filetype(load_addr: int) -> str | None:
    """
    Extract a RISC OS filetype from a load address.

    RISC OS encodes file types in load/exec address pairs.  When the top 12
    bits of the load address (bits 31:20) are all 0xF, the file is
    date-stamped and bits 19:8 hold the 12-bit filetype.

    Returns the filetype as a lowercase 3-char hex string (e.g. 'fff'),
    or None if the load address is not in date-stamped format.
    """
    if (load_addr >> 20) == 0xFFF:
        filetype = (load_addr >> 8) & 0xFFF
        return f'{filetype:03x}'
    return None


def _parse_archimedes_block(system_use: bytes) -> tuple[str | None, bool]:
    """
    Parse an ARCHIMEDES System Use block from the start of the SU area.

    The block is a raw 32-byte structure (not a standard 2-char SUSP entry):
      Offset  Size  Field
       0      10    b'ARCHIMEDES' signature
      10       4    Load address (LE uint32)
      14       4    Exec address (LE uint32)
      18       4    Attributes (LE uint32)
      22      10    Reserved

    Attributes bit 0x100 indicates the original RISC OS filename began with
    '!' (pling).  Because '!' is invalid in ISO 9660, Acorn mastering tools
    store it as '_'; this flag signals the caller to restore the '!'.

    Returns:
        (filetype_hex_or_None, has_pling_flag)
    """
    if len(system_use) < _ARCHIMEDES_MIN:
        return None, False
    if system_use[:10] != _ARCHIMEDES_SIG:
        return None, False

    load_addr, _exec_addr, attributes = struct.unpack_from('<III', system_use, 10)
    filetype = _get_riscos_filetype(load_addr)
    has_pling = bool(attributes & 0x100)
    return filetype, has_pling


def _get_nm_name(system_use: bytes) -> str | None:
    """
    Scan the System Use area for Rock Ridge NM (alternate name) entries.

    Handles the case where the ARCHIMEDES block precedes SUSP entries: SUSP
    scanning begins after the 32-byte ARCHIMEDES block if present, otherwise
    from offset 0.

    Multi-part NM entries (continuation flag set) are assembled in order.
    CE (Continuation Area Extension) entries are not followed — this covers
    the vast majority of real-world RISC OS ISOs.

    Returns the assembled name string, or None if no NM entry found.
    """
    # Start SUSP scan after the ARCHIMEDES block if it's present
    offset = _ARCHIMEDES_SIZE if (len(system_use) >= 10 and system_use[:10] == _ARCHIMEDES_SIG) else 0

    name_parts: list[bytes] = []

    while offset + 4 <= len(system_use):
        sig = system_use[offset:offset + 2]
        if len(sig) < 2 or system_use[offset] == 0:
            break
        entry_len = system_use[offset + 2]
        if entry_len < 4 or offset + entry_len > len(system_use):
            break

        if sig == b'NM':
            # NM entry: [sig(2)][len(1)][ver(1)][flags(1)][name(len-5)]
            if entry_len >= 5:
                flags = system_use[offset + 4]
                name_bytes = system_use[offset + 5:offset + entry_len]
                name_parts.append(name_bytes)
                # Bit 0 of flags means continuation; bit 1 means use current dir name
                if not (flags & 0x01):
                    break  # This is the last (or only) NM entry
        elif sig == b'CE':
            # Continuation area — not followed; stop scanning
            break

        offset += entry_len

    if not name_parts:
        return None

    try:
        return b''.join(name_parts).decode('utf-8', errors='replace')
    except Exception:
        return None


def _walk_directory(
    f,
    lba: int,
    size: int,
    display_prefix: str,
    raw_prefix: str,
    result: dict[str, str],
    rename_map: dict[str, str],
) -> None:
    """
    Recursively walk an ISO 9660 directory and populate ``result`` with
    path → RISC OS filetype mappings.

    Each file is indexed under multiple normalised path keys so that
    whatever name 7z chose to use (ISO 9660, Rock Ridge, or pling-mapped)
    can be matched against the extracted file paths.

    Two parallel prefixes are maintained:
    - ``display_prefix``: built using pling-mapped directory names (e.g.
      ``!PAINT``), for when 7z used Rock Ridge NM names that include ``!``.
    - ``raw_prefix``: built using raw ISO 9660 directory names (e.g.
      ``_PAINT``), for when 7z used plain ISO 9660 names (no Rock Ridge).

    ``rename_map`` is populated with entries mapping the lowercase raw path
    (as 7z would extract it) to the pling-corrected display path, for every
    file where the two differ.  This lets callers fix up stored paths so they
    reflect the canonical RISC OS name (e.g. ``!ARMOVIE`` not ``_ARMOVIE``).

    Args:
        f:              Open binary file handle for the ISO.
        lba:            Logical Block Address of this directory's extent.
        size:           Byte size of the directory extent.
        display_prefix: Path of this directory using pling-mapped names
                        (empty string for the root directory).
        raw_prefix:     Path of this directory using raw ISO 9660 names
                        (empty string for the root directory).
        result:         Dict to populate with {lowercase_path: filetype_hex}.
        rename_map:     Dict to populate with {lowercase_raw_path: display_path}
                        for files whose raw path differs from display path.
    """
    # Read directory data (may span multiple sectors)
    data = bytearray()
    remaining = size
    current_lba = lba
    while remaining > 0:
        sector = _read_sector(f, current_lba)
        chunk = min(_SECTOR, remaining)
        data.extend(sector[:chunk])
        remaining -= chunk
        current_lba += 1

    offset = 0
    while offset < len(data):
        record_len = data[offset]
        if record_len == 0:
            # Padding: advance to next sector boundary
            next_sector = ((offset // _SECTOR) + 1) * _SECTOR
            offset = next_sector
            continue

        if offset + record_len > len(data):
            break

        record = data[offset:offset + record_len]
        offset += record_len

        if len(record) < 34:
            continue

        len_fi = record[32]
        if len_fi == 0:
            continue

        # Skip '.' (current dir, identifier 0x00) and '..' (parent, 0x01)
        if len_fi == 1 and record[33] in (0x00, 0x01):
            continue

        file_flags = record[25]
        is_dir = bool(file_flags & 0x02)

        # ISO 9660 filename: strip version suffix (;N) and trailing dot
        try:
            raw_name = record[33:33 + len_fi].decode('ascii', errors='replace')
        except Exception:
            continue
        # Strip version suffix (';1', ';2', …)
        if ';' in raw_name:
            raw_name = raw_name[:raw_name.index(';')]
        raw_name = raw_name.rstrip('.')

        # System Use area starts after file identifier + optional padding byte.
        # ISO 9660 requires a padding byte when L_FI is even, to keep the
        # System Use area at an even byte boundary.
        su_start = 33 + len_fi + (1 if len_fi % 2 == 0 else 0)
        system_use = bytes(record[su_start:])

        filetype, has_pling = _parse_archimedes_block(system_use)
        rr_name = _get_nm_name(system_use)

        # Build the display ISO 9660 name: apply pling mapping if flagged.
        # ISO 9660 forbids '!' so Acorn mastering tools store it as '_'.
        display_name = raw_name
        if has_pling:
            if raw_name.startswith('_'):
                display_name = '!' + raw_name[1:]
            else:
                log.warning(
                    "ARCHIMEDES pling flag set but ISO 9660 name does not start "
                    "with '_' — ignoring flag for %r", raw_name
                )

        # Build full paths for each of the two parallel prefix trees.
        display_path = f'{display_prefix}/{display_name}' if display_prefix else display_name
        raw_path = f'{raw_prefix}/{raw_name}' if raw_prefix else raw_name

        if is_dir:
            dir_lba = struct.unpack_from('<I', record, 2)[0]
            dir_size = struct.unpack_from('<I', record, 10)[0]
            # If this directory itself has a pling name, record the rename now.
            # Without this, a pling directory that contains no typed files would
            # never appear in rename_map and would not be physically renamed.
            if raw_path.lower() != display_path.lower():
                rename_map[raw_path.lower()] = display_path
            # Pass display_path as new display_prefix and raw_path as new
            # raw_prefix so that both hierarchies stay in sync.
            _walk_directory(f, dir_lba, dir_size, display_path, raw_path, result, rename_map)
        else:
            if filetype is None:
                continue

            # Index filetype under all path variants (lowercased) so that
            # whichever name 7z picks for the extracted file, we find a match.
            keys: set[str] = set()

            # Pling-mapped ISO 9660 path (matches 7z output when Rock Ridge
            # names already have '!' restored, or for files at root level)
            keys.add(display_path.lower())

            # Raw ISO 9660 path (matches 7z output when no Rock Ridge is
            # present and directory names still use '_' instead of '!')
            if raw_path.lower() != display_path.lower():
                keys.add(raw_path.lower())
                # Record the rename so callers can fix up stored paths.
                # Value is the display path (pling-corrected); key is lowercase
                # raw path (as 7z would extract it without Rock Ridge).
                rename_map[raw_path.lower()] = display_path

            # Rock Ridge alternate name paths (NM entry)
            if rr_name:
                rr_display_path = f'{display_prefix}/{rr_name}' if display_prefix else rr_name
                rr_raw_path = f'{raw_prefix}/{rr_name}' if raw_prefix else rr_name
                keys.add(rr_display_path.lower())
                if rr_raw_path.lower() != rr_display_path.lower():
                    keys.add(rr_raw_path.lower())
                # Also index without ',xxx' filetype suffix (acorn='auto' strips
                # the suffix from the display_path, so we need the bare name)
                rr_base, _ = parse_acorn_filename(rr_name)
                if rr_base != rr_name:
                    rr_display_base = f'{display_prefix}/{rr_base}' if display_prefix else rr_base
                    rr_raw_base = f'{raw_prefix}/{rr_base}' if raw_prefix else rr_base
                    keys.add(rr_display_base.lower())
                    if raw_prefix != display_prefix:
                        keys.add(rr_raw_base.lower())
                        # Rename map for suffix-stripped RR path (matches
                        # enumerate_extracted_files acorn-mode display path)
                        if rr_raw_base.lower() != rr_display_base.lower():
                            rename_map[rr_raw_base.lower()] = rr_display_base

            for key in keys:
                result[key] = filetype


def parse_iso_riscos_filetypes(
    iso_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Parse an ISO 9660 image and return filetype and rename mappings derived
    from the ARCHIMEDES extension.

    Walks the Primary Volume Descriptor directory tree and extracts filetype
    information from ARCHIMEDES System Use blocks.  Also indexes each file
    under its Rock Ridge alternate name (if present) so that paths from 7z
    extraction can be matched regardless of which name 7z chose to use.

    The pling mapping ('_' → '!' for the leading character when the ARCHIMEDES
    attributes pling flag is set) is applied so that extracted paths such as
    '!Paint/!RunImage' match correctly.

    Returns:
        A 2-tuple ``(filetype_map, rename_map)``:

        ``filetype_map`` maps lowercase forward-slash-separated path strings
        to lowercase 3-char hex filetype strings.  Multiple path variants
        (ISO 9660, Rock Ridge, pling-mapped) are all indexed so that whatever
        name 7z chose for the extracted file, a lookup succeeds.

        ``rename_map`` maps the lowercase raw ISO 9660 path (as 7z would
        extract without Rock Ridge) to the pling-corrected display path, for
        every file whose raw path differs from its display path (e.g.
        ``'_armovie/sprites'`` → ``'!ARMOVIE/SPRITES'``).  Use this to fix
        the stored file path so it reflects the canonical RISC OS name.

        Both dicts are empty on any I/O or parse error (graceful degradation).
    """
    filetype_map: dict[str, str] = {}
    rename_map: dict[str, str] = {}
    try:
        with open(iso_path, 'rb') as f:
            # Primary Volume Descriptor is at sector 16
            pvd = _read_sector(f, 16)

            # Validate PVD magic
            if len(pvd) < 156 + 34:
                return filetype_map, rename_map
            if pvd[0] != 0x01 or pvd[1:6] != b'CD001':
                return filetype_map, rename_map

            # Root directory record is at PVD offset 156 (34 bytes)
            root_record = pvd[156:156 + 34]
            root_lba = struct.unpack_from('<I', root_record, 2)[0]
            root_size = struct.unpack_from('<I', root_record, 10)[0]

            _walk_directory(f, root_lba, root_size, '', '', filetype_map, rename_map)

    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        # Gracefully degrade on I/O errors and ISO parse failures
        pass

    return filetype_map, rename_map

# vim: ts=4 sw=4 et
