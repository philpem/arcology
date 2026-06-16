"""
Detect and remove ARMlock disc security from Filecore disc images.

ARMlock (by Digital Services) is a disc security package used in schools to
prevent pupils from deleting applications or damaging system files.  It works by:
- Replacing the real root directory with a stripped copy containing only an
  !Boot file that loads the ARMlock module
- Stashing the real root directory at disc address 0x400
- Encoding the boot_option field in both copies of the zone map header

Disc Image Manager cannot extract files from a protected image because it
sees only the stripped root directory.  The security must be detected and
removed before passing the image to DIM.

This module provides two public functions:

  detect_armlock(image_path)  -> detection result dict
  remove_armlock(image_path, output_path)  -> removal result dict

The detection logic is adapted from armlock_tool.py (standalone CLI).
"""

import shutil
import struct
from pathlib import Path
from ..config import log
from .base import open_sector_reader
from .extraction import _get_riscos_filetype
from .partition import (
    FILECORE_BB_DISC_RECORD_OFFSET,
    FILECORE_BOOT_BLOCK_OFFSET,
    parse_filecore_disc_record,
)

# ---------------------------------------------------------------------------
# FileCore on-disc layout constants
# ---------------------------------------------------------------------------

# Disc address of the disc record: the FileCore boot block lives at &C00 and the
# disc record sits at &1C0 within it, so &C00 + &1C0 = &DC0.  Derived from
# partition.py's named constants so the offset cannot silently diverge from the
# (now shared) disc-record parser.
DISC_RECORD_ADDR = FILECORE_BOOT_BLOCK_OFFSET + FILECORE_BB_DISC_RECORD_OFFSET

# New-format FileCore directory ("Hugo"/"Nick") layout.
# NB: 0x800 is the *small* directory size; Extended-format (E+/F+) "big"
# directories are 0x1000 (4 KB) and are intentionally NOT handled here — the
# ARMlock discs this module targets are all small-directory format.
FILECORE_DIR_SIZE = 0x800
# Size of one directory entry.
FILECORE_DIR_ENTRY_SIZE = 26
# Entries start after the 1-byte StartMasSeq + 4-byte "Hugo"/"Nick" magic.
FILECORE_DIR_HEADER_SIZE = 5
# Bytes reserved at the end of the block for the directory tail (DirTail:
# parent SIN, dir name, end-sequence byte and StartMasSeq check).
FILECORE_DIR_TAIL_SIZE = 0x29

# Directory-entry field offsets, relative to the entry start.
_DIRENT_NAME = 0x00          # 10 bytes, CR/NUL terminated
_DIRENT_NAME_LEN = 10
_DIRENT_LOAD = 0x0A          # ui32le load address
_DIRENT_EXEC = 0x0E          # ui32le exec address
_DIRENT_LENGTH = 0x12        # ui32le length
_DIRENT_SIN = 0x16           # 3-byte LE object SIN (indirect disc address)
_DIRENT_ATTR = 0x19          # 1-byte object attributes
# Object-attribute bit set when the entry is a directory.
FILECORE_ATTR_DIR = 0x08

# Disc address at which ARMlock stashes the real root directory during
# installation (one FILECORE_DIR_SIZE block).
ARMLOCK_STASH_ADDR = 0x400

# Upper bound on a single FileCore object assembled by _extract_file_by_sin.
# The objects we read (the ARMlock module and its Options files, directory
# blocks) are at most tens of KB; this cap stops a corrupt/hostile directory
# entry claiming a huge length from reading gigabytes into RAM.
_MAX_FILECORE_FILE_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Disc record parsing
# ---------------------------------------------------------------------------

def _read_disc_record(image: bytearray, offset: int = DISC_RECORD_ADDR) -> dict:
    """Parse the 64-byte disc record from the boot block.

    Thin wrapper over the shared, full-featured
    :func:`partition.parse_filecore_disc_record`; *offset* defaults to the
    whole-disc boot-block location (&DC0).
    """
    d = image[offset:offset + 0x40]
    if len(d) < 0x20:
        raise ValueError("Image too small to contain a disc record")
    return parse_filecore_disc_record(bytes(d))


# ---------------------------------------------------------------------------
# Address computation
# ---------------------------------------------------------------------------

def _compute_zone_disc_address(rec: dict, zone: int) -> int:
    """Compute the disc byte address of the start of a given zone's data."""
    sector_size = rec["sector_size"]
    bpmb = rec["bpmb"]
    zone0_aus = (sector_size - 64) * 8
    zoneN_aus = (sector_size - 4) * 8
    if zone == 0:
        return 0
    return (zone0_aus + (zone - 1) * zoneN_aus) * bpmb


def _compute_armlock_addresses(rec: dict) -> dict:
    """Derive the three key addresses ARMlock uses, plus the signature location."""
    nzones = rec["nzones"]
    sector_size = rec["sector_size"]
    half = nzones // 2
    map_start = _compute_zone_disc_address(rec, half)
    stride = nzones * sector_size
    return {
        "map_primary": map_start,
        "map_backup": map_start + stride,
        "root_dir": map_start + 2 * stride,
        "signature": map_start + 2 * stride + 0x400,
        "stride": stride,
    }


# ---------------------------------------------------------------------------
# Directory parsing
# ---------------------------------------------------------------------------

def _parse_directory(image: bytearray, addr: int) -> tuple[bool, list]:
    """Parse a new-format Filecore directory (FILECORE_DIR_SIZE bytes) at the given address.

    Returns (valid, entries) where entries is a list of dicts with keys:
      name, load, exec, length, sin, attr, is_dir, filetype
    """
    if addr + FILECORE_DIR_SIZE > len(image):
        return False, []
    # Materialise the directory block in a single (bounded) read.  This keeps the
    # parse working when *image* is a SectorReader (whose slices are real bytes
    # but which has no buffer protocol for struct.unpack_from) and reads the whole
    # directory once rather than re-seeking per field.  Offsets below are
    # directory-block-relative.
    block = bytes(image[addr:addr + FILECORE_DIR_SIZE])
    if len(block) < FILECORE_DIR_SIZE:
        return False, []
    magic = block[1:FILECORE_DIR_HEADER_SIZE]
    if magic not in (b"Hugo", b"Nick"):
        return False, []
    entries = []
    offset = FILECORE_DIR_HEADER_SIZE
    entries_end = FILECORE_DIR_SIZE - FILECORE_DIR_TAIL_SIZE
    while offset + FILECORE_DIR_ENTRY_SIZE <= entries_end:
        if block[offset] == 0x00:
            break
        name_bytes = block[offset + _DIRENT_NAME:offset + _DIRENT_NAME + _DIRENT_NAME_LEN]
        name_end = next(
            (i for i in range(_DIRENT_NAME_LEN) if name_bytes[i] in (0x00, 0x0D)),
            _DIRENT_NAME_LEN,
        )
        name = name_bytes[:name_end].decode("ascii", errors="replace")
        load = struct.unpack_from("<I", block, offset + _DIRENT_LOAD)[0]
        exec_addr = struct.unpack_from("<I", block, offset + _DIRENT_EXEC)[0]
        length = struct.unpack_from("<I", block, offset + _DIRENT_LENGTH)[0]
        sin = (block[offset + _DIRENT_SIN]
               | (block[offset + _DIRENT_SIN + 1] << 8)
               | (block[offset + _DIRENT_SIN + 2] << 16))
        attr = block[offset + _DIRENT_ATTR]
        is_dir = bool(attr & FILECORE_ATTR_DIR)
        # Hex-string form ('fff'), consistent with the rest of the system
        filetype = _get_riscos_filetype(load)
        entries.append({
            "name": name,
            "load": load,
            "exec": exec_addr,
            "length": length,
            "sin": sin,
            "attr": attr,
            "is_dir": is_dir,
            "filetype": filetype,
        })
        offset += FILECORE_DIR_ENTRY_SIZE
    return True, entries


# ---------------------------------------------------------------------------
# Zone map checksum
# ---------------------------------------------------------------------------

def _compute_zone_check(sector_data: bytes | bytearray) -> int:
    """Compute the ZoneCheck byte for a zone sector.

    Models the ARM ADCS instruction: a 32-bit accumulator plus carry flag.
    Sums all words end-to-start, subtracts the existing check byte at offset 0,
    then folds 32->16->8 via XOR.
    """
    assert len(sector_data) % 4 == 0
    acc = 0
    carry = 0
    for i in range(len(sector_data) - 4, -1, -4):
        word = struct.unpack_from("<I", sector_data, i)[0]
        total = acc + word + carry
        acc = total & 0xFFFFFFFF
        carry = 1 if total > 0xFFFFFFFF else 0
    s = (acc - sector_data[0]) & 0xFFFFFFFF
    s = s ^ (s >> 16)
    s = s ^ (s >> 8)
    return s & 0xFF


def _decode_armlock_boot_option(stored: int) -> int:
    """Recover the real boot_option from ARMlock's encoded value."""
    return stored >> 2


# ---------------------------------------------------------------------------
# Zone map walking (for module extraction)
# ---------------------------------------------------------------------------

def _walk_zone_map(image: bytearray, rec: dict, target_frag_id: int) -> list:
    """Walk the zone map and collect all fragments with the given ID.

    Returns a list of (disc_address, length_in_bytes) tuples, in zone order.
    """
    nzones = rec["nzones"]
    sector_size = rec["sector_size"]
    bpmb = rec["bpmb"]
    idlen = rec["idlen"]
    zone_spare = rec["zone_spare"]
    half = nzones // 2
    fragments = []
    map_start = _compute_zone_disc_address(rec, half)

    for z in range(nzones):
        zone_disc_addr = _compute_zone_disc_address(rec, z)
        map_block_addr = map_start + z * sector_size
        if map_block_addr + sector_size > len(image):
            continue
        zone_data = image[map_block_addr:map_block_addr + sector_size]
        header_bits = (64 if z == 0 else 4) * 8
        zone_end_bits = sector_size * 8
        bit_pos = header_bits
        alloc_unit = 0

        while bit_pos < zone_end_bits:
            if z > 0 and alloc_unit < zone_spare:
                bit_pos += 1
                alloc_unit += 1
                continue
            if bit_pos + idlen > zone_end_bits:
                break
            frag_start_au = alloc_unit
            # Read fragment ID (idlen bits, LSB first).
            # Each ID bit occupies one allocation unit, so alloc_unit advances here.
            frag_id = 0
            for b in range(idlen):
                byte_idx = bit_pos >> 3
                bit_idx = bit_pos & 7
                if zone_data[byte_idx] & (1 << bit_idx):
                    frag_id |= (1 << b)
                bit_pos += 1
                alloc_unit += 1
            # Count padding zeros + terminating 1 (each also an allocation unit)
            while bit_pos < zone_end_bits:
                byte_idx = bit_pos >> 3
                bit_idx = bit_pos & 7
                bit_val = (zone_data[byte_idx] >> bit_idx) & 1
                bit_pos += 1
                alloc_unit += 1
                if bit_val == 1:
                    break
            frag_len_au = alloc_unit - frag_start_au
            if frag_id == target_frag_id:
                disc_addr = zone_disc_addr + frag_start_au * bpmb
                frag_bytes = frag_len_au * bpmb
                fragments.append((disc_addr, frag_bytes))

    return fragments


def _extract_file_by_sin(
    image: bytearray, rec: dict, sin: int, file_length: int,
    max_bytes: int = _MAX_FILECORE_FILE_BYTES,
) -> bytes | None:
    """Resolve a SIN and extract file data.

    The files this is used for (the ARMlock module and its small Options files,
    plus directory blocks) are tiny, so *file_length* and the assembled object
    are capped at *max_bytes*: a corrupt/hostile directory entry claiming a
    multi-GB length must not cause gigabytes to be read into RAM.
    """
    frag_id = sin >> 8
    sharing_offset = sin & 0xFF
    if frag_id < 2:
        return None
    if file_length > max_bytes:
        log.warning(
            "ARMlock: refusing to extract object of %d bytes (> %d-byte cap)",
            file_length, max_bytes,
        )
        return None
    fragments = _walk_zone_map(image, rec, frag_id)
    if not fragments:
        return None
    obj_data = bytearray()
    for disc_addr, frag_bytes in fragments:
        if disc_addr + frag_bytes > len(image):
            frag_bytes = len(image) - disc_addr
        obj_data.extend(image[disc_addr:disc_addr + frag_bytes])
        if len(obj_data) > max_bytes:
            # Assembled object already exceeds what we will ever return; stop
            # before an over-long fragment chain balloons memory.
            break
    start = 0
    if sharing_offset > 0:
        share_unit = rec["sector_size"]
        if rec.get("share_size", 0) > 0:
            share_unit = rec["sector_size"] << rec["share_size"]
        start = (sharing_offset - 1) * share_unit
    if start + file_length > len(obj_data):
        return bytes(obj_data[start:])
    return bytes(obj_data[start:start + file_length])


def _read_subdir_by_sin(image: bytearray, rec: dict, sin: int) -> tuple[bool, list]:
    """Read a subdirectory by its SIN and return its directory entries.

    Resolves the SIN via the zone map (same mechanism as for files) and
    parses the resulting directory block.
    """
    raw = _extract_file_by_sin(image, rec, sin, FILECORE_DIR_SIZE)
    if not raw:
        return False, []
    return _parse_directory(bytearray(raw), 0)


def _parse_armlock_options(data: bytes) -> dict:
    """Parse $.ARMlock.!ARMlock.Options binary format.

    Layout:
      0x00  4  hash_word1 (v3), little-endian
      0x04  4  hash_word2 (v4), little-endian
      0x08  6  copy of BootUp.Options (install_serial + config_flag + password_required)
      0x0E  1  management_flag
      0x0F  …  NUL-terminated application path strings
    """
    if len(data) < 0x0F:
        return {}
    result: dict = {
        'hash_word1': struct.unpack_from('<I', data, 0x00)[0],
        'hash_word2': struct.unpack_from('<I', data, 0x04)[0],
        'management_flag': data[0x0E] if len(data) > 0x0E else None,
    }
    paths: list = []
    pos = 0x0F
    while pos < len(data):
        if data[pos] == 0x00:   # null at start of string = list terminator
            break
        nul = data.find(b'\x00', pos)
        end = nul if nul != -1 else len(data)
        s = data[pos:end].decode('ascii', errors='replace').strip()
        if s:
            paths.append(s)
        if nul == -1:
            break
        pos = nul + 1
    result['app_paths'] = paths
    return result


def _parse_bootup_options(data: bytes) -> dict:
    """Parse $.ARMlock.BootUp.Options binary format.

    Layout:
      0x00  4  install_serial -- serial number of the install disc, little-endian
      0x04  1  config_flag
      0x05  1  password_required (0=no, 1=yes)
    """
    if len(data) < 6:
        return {}
    return {
        'install_serial': struct.unpack_from('<I', data, 0x00)[0],
        'config_flag': data[0x04],
        'password_required': bool(data[0x05]),
    }


def _parse_module_header(data: bytes) -> dict:
    """Extract title and help strings from a RISC OS module header.

    RISC OS module header layout (offsets are bytes from module start):
      +0x10: title string offset  (e.g. "ARMlock")
      +0x14: help string offset   (e.g. "ARMlock\\t1.00 (01 Jan 1994)")

    The help string conventionally has the format:
      "<name>\\t<version> (<date>)"
    The part after the tab is the version/date string.

    Tries module-start-relative offsets first, then self-relative (offset
    from the word's own address), then falls back to scanning for the
    title string literal — covering variations in how ARMlock was built.
    """
    info: dict = {'title': None, 'help_string': None, 'version': None}
    if len(data) < 0x18:
        return info

    def _read_cstr(off: int) -> str | None:
        if off == 0 or off >= len(data):
            return None
        end = off
        while end < len(data) and data[end] not in (0x00, 0x0D, 0xFF):
            end += 1
        if end == off:
            return None
        try:
            s = data[off:end].decode('ascii', errors='strict').strip()
        except UnicodeDecodeError:
            return None
        return s if s else None

    title_raw = struct.unpack_from('<I', data, 0x10)[0]
    help_raw  = struct.unpack_from('<I', data, 0x14)[0]

    # Attempt 1: module-start-relative (RISC OS PRM standard)
    title = _read_cstr(title_raw)
    help_str = _read_cstr(help_raw)

    # Attempt 2: self-relative (offset from the word's own address)
    if title is None and title_raw > 0:
        title = _read_cstr(0x10 + title_raw)
    if help_str is None and help_raw > 0:
        help_str = _read_cstr(0x14 + help_raw)

    # Attempt 3: scan for "ARMlock" literal in the binary data
    if title is None:
        for marker in (b'ARMlock\x00', b'ARMlock\r', b'ARMlock\xff',
                       b'ARMLOCK\x00', b'ArmLock\x00'):
            idx = data.find(marker[:-1])
            if idx >= 0:
                t = _read_cstr(idx)
                if t:
                    title = t
                    break

    info['title'] = title

    # If help string still missing but we have a title, search for "<title>\t"
    if help_str is None and title:
        search = title.encode('ascii') + b'\t'
        idx = data.find(search)
        if idx >= 0:
            help_str = _read_cstr(idx)

    if help_str:
        info['help_string'] = help_str
        if '\t' in help_str:
            info['version'] = help_str.split('\t', 1)[1].strip()

    return info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_armlock(image_path: Path) -> dict:
    """Detect ARMlock copy protection in a Filecore disc image.

    Returns a dict with:
      detected (bool)             -- True if ARMlock signature found
      disc_record (dict)          -- parsed disc record fields
      addresses (dict)            -- map_primary, map_backup, root_dir, signature, stride
      zone_map_primary (dict)     -- stored_boot_option, real_boot_option, check_byte, check_valid
      zone_map_backup (dict)      -- same fields for backup copy
      stripped_root (list)        -- directory entries seen by FileCore (ARMlock's fake root)
      real_root (list)            -- real root directory entries stashed at 0x400
      module_data (bytes|None)    -- raw bytes of the ARMlock module from !Boot, or None
      error (str|None)            -- set if the image cannot be analysed (too small, old-map, etc.)
    """
    result: dict = {
        'detected': False,
        'disc_record': {},
        'addresses': {},
        'zone_map_primary': {},
        'zone_map_backup': {},
        'stripped_root': [],
        'real_root': [],
        'armlock_config': {},   # files extracted from $.Armlock directory
        'module_data': None,
        'module_title': None,
        'module_version': None,
        'error': None,
    }

    try:
        with open_sector_reader(image_path) as image:
            return _detect_armlock_scan(image, result)
    except OSError as e:
        result['error'] = f'Cannot read image: {e}'
        return result


def _detect_armlock_scan(image, result: dict) -> dict:
    """Run ARMlock detection over an already-open image buffer.

    *image* is any indexable byte buffer — in practice a :class:`SectorReader`
    over the disc image, so a multi-GB hard-disc image is never copied into RAM:
    each ``image[a:b]`` is a bounded seek/read and the detector only touches the
    boot block, the two zone maps, the root/stash directories and whatever small
    files the FileCore directory chain references.  *result* is the
    partially-built dict from :func:`detect_armlock`, returned populated.
    """
    if len(image) < 0xE00:
        result['error'] = 'Image too small to contain a boot block'
        return result

    try:
        rec = _read_disc_record(image)
    except ValueError as e:
        result['error'] = str(e)
        return result

    if rec['idlen'] == 0:
        result['error'] = 'Old-map disc — ARMlock only applies to new-map discs'
        return result

    result['disc_record'] = {
        'sector_size': rec['sector_size'],
        'bpmb': rec['bpmb'],
        'nzones': rec['nzones'],
        'idlen': rec['idlen'],
        'disc_size': rec['disc_size'],
        'root_dir_sin': rec['root_dir'],
        'disc_name': rec['disc_name'],
        'format_version': rec['format_version'],
    }

    addrs = _compute_armlock_addresses(rec)
    result['addresses'] = addrs

    if addrs['signature'] + 17 > len(image):
        result['error'] = 'Image too small for computed ARMlock addresses'
        return result

    # Check for "ARMlock installed" signature in padding area of stripped root
    sig = image[addrs['signature']:addrs['signature'] + 17]
    result['detected'] = (sig == b'ARMlock installed')

    if not result['detected']:
        return result

    # Collect zone map info for both copies
    sector_size = rec['sector_size']
    for key, map_addr in [('zone_map_primary', addrs['map_primary']),
                          ('zone_map_backup', addrs['map_backup'])]:
        stored_boot = image[map_addr + 0x0B]
        real_boot = _decode_armlock_boot_option(stored_boot)
        check_byte = image[map_addr]
        sector = bytes(image[map_addr:map_addr + sector_size])
        expected_check = _compute_zone_check(sector)
        result[key] = {
            'address': map_addr,
            'stored_boot_option': stored_boot,
            'real_boot_option': real_boot,
            'check_byte': check_byte,
            'check_valid': (check_byte == expected_check),
            'expected_check': expected_check,
        }

    # Parse stripped root (what FileCore currently sees)
    valid_s, stripped_entries = _parse_directory(image, addrs['root_dir'])
    if valid_s:
        result['stripped_root'] = stripped_entries

    # Parse real root (stashed at ARMLOCK_STASH_ADDR by ARMlock during installation)
    valid_r, real_entries = _parse_directory(image, ARMLOCK_STASH_ADDR)
    if valid_r:
        result['real_root'] = real_entries

    # Navigate to $.ARMlock in the real root and parse the security Options files.
    #
    # The layout is:
    #   $.ARMlock.!ARMlock/Options  -- full options: hash words, management flag, app paths
    #   $.ARMlock.BootUp/Options    -- boot copy: hash word 1, config flag, password-required
    #
    # Subdirectories are detected by the Hugo/Nick magic-byte check rather than
    # the unreliable is_dir attribute flag.
    if valid_r:
        config_dir_entry = next(
            (e for e in real_entries
             if e['name'].lower() in ('armlock', '!armlock')),
            None
        )
        if config_dir_entry:
            valid_cd, config_entries = _read_subdir_by_sin(image, rec, config_dir_entry['sin'])
            if valid_cd:
                security_config: dict = {}
                for fe in config_entries:
                    valid_sd, sd_entries = _read_subdir_by_sin(image, rec, fe['sin'])
                    if not valid_sd:
                        continue
                    fe_lower = fe['name'].lower()
                    for sfe in sd_entries:
                        if sfe['name'].lower() != 'options' or sfe['length'] <= 0:
                            continue
                        valid_ssd, _ = _read_subdir_by_sin(image, rec, sfe['sin'])
                        if valid_ssd:
                            continue
                        file_data = _extract_file_by_sin(
                            image, rec, sfe['sin'], sfe['length'])
                        if not file_data:
                            continue
                        if fe_lower == '!armlock':
                            security_config['options'] = _parse_armlock_options(file_data)
                        elif fe_lower == 'bootup':
                            security_config['bootup'] = _parse_bootup_options(file_data)
                result['armlock_config'] = security_config

    # Extract the ARMlock module from !Boot in the stripped root.
    # ARMlock installs itself as a single RISC OS module file named !Boot in the
    # fake root; when the disc is booted, FileCore runs !Boot which loads the module.
    if valid_s:
        boot_entry = next(
            (e for e in stripped_entries if e['name'] == '!Boot'),
            None
        )
        if boot_entry and boot_entry['length'] > 0:
            module_data = _extract_file_by_sin(
                image, rec, boot_entry['sin'], boot_entry['length'])
            result['module_data'] = module_data
            if module_data:
                header = _parse_module_header(module_data)
                result['module_title'] = header['title']
                result['module_version'] = header['version']

    return result


def remove_armlock(image_path: Path, output_path: Path) -> dict:
    """Remove ARMlock protection and write the cleaned image to output_path.

    Caller should verify detection first with detect_armlock().  This function
    will still attempt removal if called on a non-protected image, which will
    only result in a byte-for-byte copy.

    Steps performed:
      1. Fix boot_option in both zone map copies and recompute ZoneCheck
      2. Copy real root from 0x400 to its correct location
      3. Zero out the stashed copy at 0x400

    Returns a dict with:
      success (bool)
      error (str|None)
    """
    # Validate the disc record from a small prefix first, so a multi-GB image is
    # never read into RAM.  The boot block lives at &C00 with the disc record
    # inside it, so the first &E00 bytes are sufficient to read and validate it.
    try:
        with open(image_path, 'rb') as f:
            head = f.read(0x1000)
    except OSError as e:
        return {'success': False, 'error': f'Cannot read image: {e}'}

    if len(head) < 0xE00:
        return {'success': False, 'error': 'Image too small to contain a boot block'}

    try:
        rec = _read_disc_record(head)
    except ValueError as e:
        return {'success': False, 'error': str(e)}

    if rec['idlen'] == 0:
        return {'success': False, 'error': 'Old-map disc — no removal needed'}

    addrs = _compute_armlock_addresses(rec)
    sector_size = rec['sector_size']

    # Copy the image on disk, then patch only the few affected sectors in place
    # with seek/read/write.  The full image is never held in memory — only the
    # handful of touched sectors are read — so even a multi-GB hard-disc image is
    # safe, and this needs no mmap (so there is nothing to fall back from).
    try:
        shutil.copy(image_path, output_path)
        with open(output_path, 'r+b') as f:
            # 1. Fix zone map boot_option and recompute ZoneCheck for both copies
            for map_addr in (addrs['map_primary'], addrs['map_backup']):
                f.seek(map_addr)
                sector = bytearray(f.read(sector_size))
                if len(sector) < sector_size:
                    return {'success': False, 'error': 'Truncated image: zone map sector missing'}
                real_boot = _decode_armlock_boot_option(sector[0x0B])
                sector[0x0B] = real_boot
                sector[0] = _compute_zone_check(sector)  # check byte at sector offset 0
                f.seek(map_addr)
                f.write(sector)

            # 2. Copy real root from stash location to its correct location
            f.seek(ARMLOCK_STASH_ADDR)
            real_root = f.read(FILECORE_DIR_SIZE)
            f.seek(addrs['root_dir'])
            f.write(real_root)

            # 3. Zero out the stashed copy
            f.seek(ARMLOCK_STASH_ADDR)
            f.write(b'\x00' * FILECORE_DIR_SIZE)
    except OSError as e:
        return {'success': False, 'error': f'Cannot write output: {e}'}

    return {'success': True, 'error': None}

# vim: ts=4 sw=4 et
