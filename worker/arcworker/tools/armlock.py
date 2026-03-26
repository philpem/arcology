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

import struct
from pathlib import Path


# ---------------------------------------------------------------------------
# Disc record parsing
# ---------------------------------------------------------------------------

def _read_disc_record(image: bytearray, offset: int = 0xDC0) -> dict:
    """Parse the 64-byte disc record from the boot block."""
    d = image[offset:offset + 0x40]
    if len(d) < 0x20:
        raise ValueError("Image too small to contain a disc record")
    rec = {
        "log2_sector_size": d[0x00],
        "sectors_per_track": d[0x01],
        "heads": d[0x02],
        "density": d[0x03],
        "idlen": d[0x04],
        "log2_bpmb": d[0x05],
        "skew": d[0x06],
        "boot_option": d[0x07],
        "low_sector": d[0x08],
        "nzones_lo": d[0x09],
        "zone_spare": struct.unpack_from("<H", d, 0x0A)[0],
        "root_dir": struct.unpack_from("<I", d, 0x0C)[0],
        "disc_size_lo": struct.unpack_from("<I", d, 0x10)[0],
    }
    # Extended fields (RISC OS 3.6+)
    if len(d) >= 0x30:
        rec["disc_id"] = struct.unpack_from("<H", d, 0x14)[0]
        rec["disc_name"] = d[0x16:0x20].rstrip(b"\x00").decode("ascii", errors="replace")
        rec["disc_size_hi"] = struct.unpack_from("<I", d, 0x24)[0]
        rec["share_size"] = d[0x28]
        rec["big_flag"] = d[0x29]
        rec["nzones_hi"] = d[0x2A]
        rec["format_version"] = struct.unpack_from("<I", d, 0x2C)[0]
    else:
        rec["disc_name"] = ""
        rec["disc_size_hi"] = 0
        rec["nzones_hi"] = 0
        rec["format_version"] = 0
    rec["nzones"] = rec["nzones_lo"] | (rec["nzones_hi"] << 8)
    rec["disc_size"] = rec["disc_size_lo"] | (rec["disc_size_hi"] << 32)
    rec["sector_size"] = 1 << rec["log2_sector_size"]
    rec["bpmb"] = 1 << rec["log2_bpmb"]
    return rec


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
    """Parse a new-format Filecore directory (0x800 bytes) at the given address.

    Returns (valid, entries) where entries is a list of dicts with keys:
      name, load, exec, length, sin, attr, is_dir, filetype
    """
    if addr + 0x800 > len(image):
        return False, []
    seq = image[addr]
    magic = image[addr + 1:addr + 5]
    if magic not in (b"Hugo", b"Nick"):
        return False, []
    entries = []
    offset = addr + 5
    while offset + 26 <= addr + 0x800 - 0x29:
        if image[offset] == 0x00:
            break
        name_bytes = image[offset:offset + 10]
        name_end = next((i for i in range(10) if name_bytes[i] in (0x00, 0x0D)), 10)
        name = name_bytes[:name_end].decode("ascii", errors="replace")
        load = struct.unpack_from("<I", image, offset + 0x0A)[0]
        exec_addr = struct.unpack_from("<I", image, offset + 0x0E)[0]
        length = struct.unpack_from("<I", image, offset + 0x12)[0]
        sin = image[offset + 0x16] | (image[offset + 0x17] << 8) | (image[offset + 0x18] << 16)
        attr = image[offset + 0x19]
        is_dir = bool(attr & 0x08)
        filetype = None
        if (load >> 20) == 0xFFF:
            filetype = (load >> 8) & 0xFFF
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
        offset += 26
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


def _extract_file_by_sin(image: bytearray, rec: dict, sin: int, file_length: int) -> bytes | None:
    """Resolve a SIN and extract file data."""
    frag_id = sin >> 8
    sharing_offset = sin & 0xFF
    if frag_id < 2:
        return None
    fragments = _walk_zone_map(image, rec, frag_id)
    if not fragments:
        return None
    obj_data = bytearray()
    for disc_addr, frag_bytes in fragments:
        if disc_addr + frag_bytes > len(image):
            frag_bytes = len(image) - disc_addr
        obj_data.extend(image[disc_addr:disc_addr + frag_bytes])
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
    parses the resulting 0x800-byte directory block.
    """
    raw = _extract_file_by_sin(image, rec, sin, 0x800)
    if not raw:
        return False, []
    return _parse_directory(bytearray(raw), 0)


# ---------------------------------------------------------------------------
# RISC OS module header parsing
# ---------------------------------------------------------------------------

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
        image = bytearray(image_path.read_bytes())
    except OSError as e:
        result['error'] = f'Cannot read image: {e}'
        return result

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

    # Parse real root (stashed at 0x400 by ARMlock during installation)
    valid_r, real_entries = _parse_directory(image, 0x400)
    if valid_r:
        result['real_root'] = real_entries

    # Navigate to $.Armlock (or $.!Armlock) in the real root.
    # This directory holds the security configuration: settings, serial
    # number and password hashes.  Extract all small files (≤ 4 KB) as
    # hex so they can be displayed without storing huge amounts of data.
    #
    # The is_dir attribute flag can be unreliable, so we match the entry by
    # name only.  Inside $.ARMlock the layout is typically:
    #   $.ARMlock.!ARMlock/   -- ARMlock application (settings, options)
    #   $.ARMlock.BootUp/     -- Boot configuration files
    # We detect subdirectories by attempting to parse them as ADFS directories
    # (Hugo/Nick magic check) rather than relying on is_dir.
    if valid_r:
        config_dir_entry = next(
            (e for e in real_entries
             if e['name'].lower() in ('armlock', '!armlock')),
            None
        )
        if config_dir_entry:
            valid_cd, config_entries = _read_subdir_by_sin(image, rec, config_dir_entry['sin'])
            if valid_cd:
                config_files: dict = {}
                for fe in config_entries:
                    # Try to read as a subdirectory (Hugo/Nick magic confirms it)
                    valid_sd, sd_entries = _read_subdir_by_sin(image, rec, fe['sin'])
                    if valid_sd:
                        prefix = fe['name'] + '/'
                        for sfe in sd_entries:
                            if sfe['length'] <= 0:
                                continue
                            valid_ssd, _ = _read_subdir_by_sin(image, rec, sfe['sin'])
                            if valid_ssd:
                                continue
                            if sfe['length'] <= 4096:
                                file_data = _extract_file_by_sin(
                                    image, rec, sfe['sin'], sfe['length'])
                                if file_data:
                                    config_files[prefix + sfe['name']] = {
                                        'length': sfe['length'],
                                        'filetype': sfe['filetype'],
                                        'hex': file_data.hex(),
                                    }
                    elif 0 < fe['length'] <= 4096:
                        valid_as_dir, _ = _read_subdir_by_sin(image, rec, fe['sin'])
                        if not valid_as_dir:
                            file_data = _extract_file_by_sin(
                                image, rec, fe['sin'], fe['length'])
                            if file_data:
                                config_files[fe['name']] = {
                                    'length': fe['length'],
                                    'filetype': fe['filetype'],
                                    'hex': file_data.hex(),
                                }
                result['armlock_config'] = config_files

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
    try:
        image = bytearray(image_path.read_bytes())
    except OSError as e:
        return {'success': False, 'error': f'Cannot read image: {e}'}

    if len(image) < 0xE00:
        return {'success': False, 'error': 'Image too small to contain a boot block'}

    try:
        rec = _read_disc_record(image)
    except ValueError as e:
        return {'success': False, 'error': str(e)}

    if rec['idlen'] == 0:
        return {'success': False, 'error': 'Old-map disc — no removal needed'}

    addrs = _compute_armlock_addresses(rec)
    sector_size = rec['sector_size']

    # 1. Fix zone map boot_option and recompute ZoneCheck for both copies
    for map_addr in (addrs['map_primary'], addrs['map_backup']):
        stored_boot = image[map_addr + 0x0B]
        real_boot = _decode_armlock_boot_option(stored_boot)
        image[map_addr + 0x0B] = real_boot
        sector = bytearray(image[map_addr:map_addr + sector_size])
        sector[0x0B] = real_boot
        new_check = _compute_zone_check(sector)
        image[map_addr] = new_check

    # 2. Copy real root from stash location (0x400) to correct location
    real_root = bytes(image[0x400:0x400 + 0x800])
    image[addrs['root_dir']:addrs['root_dir'] + 0x800] = real_root

    # 3. Zero out the stashed copy
    image[0x400:0x400 + 0x800] = b'\x00' * 0x800

    try:
        output_path.write_bytes(image)
    except OSError as e:
        return {'success': False, 'error': f'Cannot write output: {e}'}

    return {'success': True, 'error': None}

# vim: ts=4 sw=4 et
