"""
Partition detection tools.

Tools for detecting partitions and filesystems in raw disc images.
Supports:
- sfdisk - Standard MBR/GPT partition tables
- Acorn partitioning schemes (ICS/Baildon IDEFS, HCCS, Simtec, and future additions)
- ADFS signature detection - Acorn ADFS filesystem heuristics
- file command - Generic format identification

All partitioning scheme detectors normalise their output to byte-based
offsets (start_byte / size_bytes) so that the downstream carving and
gap-detection code in analysis.py doesn't need to care about sectors,
CHS geometry, or any other addressing mode.

Adding a new Acorn partition scheme
-----------------------------------
1. Write a ``_detect_<scheme>`` function that returns a dict with at
   least ``detected`` (bool) and ``partitions`` (list of dicts with
   ``index``, ``start_byte``, ``size_bytes``, ``filesystem``).
2. Call it from ``detect_acorn_partitions()`` in the appropriate
   priority order.
"""

import json
import struct
from pathlib import Path

from .base import run_tool_with_output, tool_result
from ..config import log


# =========================================================================
# Filecore disc-record helpers (shared by HCCS, Simtec, future schemes)
# =========================================================================

# Absolute disc offset of the Filecore boot block (512 bytes)
FILECORE_BOOT_BLOCK_OFFSET = 0xC00

# Size of the Filecore boot block
FILECORE_BOOT_BLOCK_SIZE = 0x200

# Offset of the hardware-dependent information field within the
# Filecore boot block.  Partition schemes (HCCS, Simtec, etc.) store
# their magic and metadata here.
FILECORE_BB_HWDEP_OFFSET = 0x1B0

# Offset of the disc record within the boot block
FILECORE_BB_DISC_RECORD_OFFSET = 0x1C0

# Disc record field offsets (relative to disc record start)
_DR_DISC_SIZE = 0x10      # ui32le: disc/partition size in bytes
_DR_DISC_NAME = 0x16      # 10 bytes, CR or null terminated


def _validate_filecore_boot_block_checksum(boot_block: bytes) -> bool:
    """Validate a 512-byte FileCore boot block checksum.

    The checksum byte at offset 0x1FF is computed by walking backwards from
    byte 0x1FE to 0x000, accumulating each byte with 8-bit carry propagation,
    and storing the result at 0x1FF.  Simple modular addition (sum % 256)
    gives a different result whenever the running total carries, so it must
    not be used here.

    Reference: partition_ics_idefs.md, "Boot Block Checksum" section.
    """
    if len(boot_block) < FILECORE_BOOT_BLOCK_SIZE:
        return False
    s = 0
    for i in range(510, -1, -1):
        s += boot_block[i]
        s = (s & 0xFF) + (s >> 8)
    return (s & 0xFF) == boot_block[511]


# =========================================================================
# SJ Research Nexus Disc Sharer constants
# =========================================================================

# Absolute byte offset of the Nexus partition table on disc
NEXUS_TABLE_OFFSET = 0x20000

# Four-byte magic at offset 0 of the partition table
NEXUS_TABLE_MAGIC = b'Net1'

# Size of the partition table header
NEXUS_TABLE_HEADER_SIZE = 16

# Size of each partition entry
NEXUS_TABLE_ENTRY_SIZE = 16

# Maximum partition entries: 240 data bytes / 16 bytes each
NEXUS_TABLE_MAX_ENTRIES = 15

# Sector size (bytes) assumed for all Nexus disc images
NEXUS_SECTOR_SIZE = 512

# Filesystem label used for Printer partitions (flag bit 3, mask 0x08).
# Printer partitions are not Filecore formatted; they contain print-spool
# data only and cannot be decoded as ADFS.
# • 'other' — register as a downloadable artefact, skip ADFS extraction (default)
# • None    — omit printer partitions from the partition list entirely
#             (they will appear as unpartitioned gaps in the output)
# • any str — use that string as the filesystem label without checking
NEXUS_PRINTER_FILESYSTEM = 'other'


# =========================================================================
# ICS / Baildon Electronics partition table constants
# =========================================================================
# "IDEFS" was used by several RISC OS IDE manufacturers; function and
# constant names use the "ICS" prefix to unambiguously identify the
# ICS/Baildon/APDL variant documented in partition_ics_idefs.md.

# Sector size is always 512 bytes for ICS IDEFS
ICS_SECTOR_SIZE = 512

# Partition table occupies sector 0 (first 512 bytes of the drive)
ICS_PARTITION_TABLE_SIZE = 512

# Maximum RISC OS partitions supported by ICS IDEFS
ICS_MAX_PARTITIONS = 4

# Checksum seed: ASCII "Part" as a little-endian uint32
ICS_CHECKSUM_SEED = 0x50617274

# Offset of total disc capacity (uint32le, in sectors) within sector 0
ICS_TOTAL_CAPACITY_OFFSET = 0x1F8

# Offset of checksum (uint32le) within sector 0
ICS_CHECKSUM_OFFSET = 0x1FC

# Size of each partition entry (two uint32le: start_sector, size_sectors)
ICS_ENTRY_SIZE = 8

# Maximum number of partition entries that fit before the capacity/checksum
# fields: 504 bytes / 8 bytes per entry = 63
ICS_MAX_ENTRIES = ICS_TOTAL_CAPACITY_OFFSET // ICS_ENTRY_SIZE

# Offset of the ICS protection flags byte within the boot block
ICS_PROTECTION_OFFSET = 0x1A7

# Offset of the password hash words within the boot block (2 x uint32le)
ICS_PASSWORD_HASH_OFFSET = 0x1A8


def _decode_nexus_flags(flags: int) -> dict:
    """Decode a Nexus partition flags byte into a human-readable dict."""
    return {
        'writable': bool(flags & 0x01),
        'multiple': bool(flags & 0x02),
        'fixed':    bool(flags & 0x04),
        'printer':  bool(flags & 0x08),
        'local':    bool(flags & 0x10),
        'last':     bool(flags & 0x80),
        'raw':      f'0x{flags:02X}',
    }


def _parse_filecore_disc_record(disc_record: bytes) -> dict:
    """
    Parse a Filecore disc record into a dict of useful fields.

    Args:
        disc_record: At least 64 bytes starting at the disc record
                     (boot_block[0x1C0:0x200]).

    Returns:
        Dict with ``disc_size``, ``disc_name``, and raw ``log2_sector_size``.
    """
    if len(disc_record) < 0x14:
        return {'disc_size': 0, 'disc_name': ''}

    disc_size = struct.unpack_from('<I', disc_record, _DR_DISC_SIZE)[0]

    disc_name = ''
    if len(disc_record) >= _DR_DISC_NAME + 10:
        raw = disc_record[_DR_DISC_NAME:_DR_DISC_NAME + 10]
        # Terminated by CR (0x0D) or null
        for terminator in (b'\r', b'\x00'):
            idx = raw.find(terminator)
            if idx != -1:
                raw = raw[:idx]
                break
        disc_name = raw.decode('latin-1', errors='replace').strip()

    return {
        'disc_size': disc_size,
        'disc_name': disc_name,
        'log2_sector_size': disc_record[0] if disc_record else 0,
    }


def _is_valid_filecore_disc_record(disc_record: bytes) -> bool:
    """
    Return True if *disc_record* looks like a plausible Filecore disc record.

    Two fields are checked:

    * ``log2_sector_size`` (byte 0): must be one of 8, 9, 10, or 12,
      corresponding to 256, 512, 1024, or 4096-byte sectors.  4096-byte
      sectors appear on Extended Format drives.  Any other value is not
      used by a real Filecore volume and almost certainly indicates that
      the boot block passed the checksum by coincidence.

    * ``disc_size`` (LE uint32 at offset 0x10): must be non-zero.

    These two checks together reduce the false-positive probability from
    the checksum's 1-in-256 to roughly 1-in-20,000.
    """
    if len(disc_record) < 0x14:
        return False
    if disc_record[0] not in (8, 9, 10, 12):
        return False
    disc_size = struct.unpack_from('<I', disc_record, _DR_DISC_SIZE)[0]
    return disc_size > 0


def _is_valid_filecore_disc_record_strict(boot_block: bytes) -> bool:
    """Return True if *boot_block* contains a plausible Filecore disc record.

    This is a stricter variant of :func:`_is_valid_filecore_disc_record`
    intended for the fallback path where the boot block checksum has failed.
    It compensates for the missing 1-in-256 checksum filter by validating
    additional disc record fields.

    *boot_block* must be the full 512-byte boot block (starting at disc
    address &C00), not just the disc record portion.
    """
    if len(boot_block) < FILECORE_BOOT_BLOCK_SIZE:
        return False

    disc_record = boot_block[FILECORE_BB_DISC_RECORD_OFFSET:]

    # Base checks: log2_sector_size in {8, 9, 10, 12} and disc_size > 0
    if not _is_valid_filecore_disc_record(disc_record):
        return False

    log2_sector_size = disc_record[0]
    sectors_per_track = disc_record[1]
    heads = disc_record[2]

    # Both fields must be non-zero on any real disc geometry.
    if sectors_per_track == 0 or heads == 0:
        return False

    # disc_size must be a whole number of sectors.
    disc_size = struct.unpack_from('<I', disc_record, _DR_DISC_SIZE)[0]
    if disc_size % (1 << log2_sector_size) != 0:
        return False

    return True


# =========================================================================
# ICS / Baildon Electronics partition detection
# =========================================================================

def _detect_ics_partitions(input_path: Path) -> dict:
    """
    Detect ICS/Baildon Electronics IDEFS partitions.

    The ICS IDEFS partition table occupies sector 0 of the physical drive.
    It contains up to 63 eight-byte entries (start_sector + size_sectors),
    a total capacity field at offset 0x1F8, and a "Part"-seeded checksum
    at offset 0x1FC.  Each valid partition has a FileCore boot block at
    partition_start + 0xC00 with protection flags and password hashes.

    Returns:
        Dict with ``detected``, ``scheme``, ``partitions`` list.
    """
    file_size = input_path.stat().st_size
    if file_size < ICS_PARTITION_TABLE_SIZE:
        return {'detected': False, 'scheme': 'ics_idefs'}

    with open(input_path, 'rb') as f:
        sector0 = f.read(ICS_PARTITION_TABLE_SIZE)

    if len(sector0) < ICS_PARTITION_TABLE_SIZE:
        return {'detected': False, 'scheme': 'ics_idefs'}

    # Validate the "Part" checksum — this is the primary identification
    if not _validate_ics_checksum(sector0):
        return {'detected': False, 'scheme': 'ics_idefs'}

    # Total disc capacity in sectors (informational)
    total_capacity_sectors = struct.unpack_from(
        '<I', sector0, ICS_TOTAL_CAPACITY_OFFSET
    )[0]

    # Parse partition entries
    partitions = []
    valid_count = 0

    for entry_idx in range(ICS_MAX_ENTRIES):
        if valid_count >= ICS_MAX_PARTITIONS:
            break

        offset = entry_idx * ICS_ENTRY_SIZE
        start_sector, size_sectors = struct.unpack_from(
            '<II', sector0, offset
        )

        # Zero size = end-of-table marker
        if size_sectors == 0:
            break

        # Bit 31 set = deleted/unused slot — skip and continue
        if size_sectors & 0x80000000:
            continue

        start_byte = start_sector * ICS_SECTOR_SIZE
        size_bytes = size_sectors * ICS_SECTOR_SIZE

        # Clamp to remaining image
        if start_byte + size_bytes > file_size:
            size_bytes = file_size - start_byte

        partition_info = {
            'index': valid_count,
            'start_byte': start_byte,
            'size_bytes': size_bytes,
            'start_sector': start_sector,
            'size_sectors': size_sectors,
            'filesystem': 'adfs',
            'scheme': 'ics_idefs',
            'description': f'ICS IDEFS partition {valid_count}',
            'disc_name': '',
            'boot_block_valid': False,
            'protection': None,
            'password_hash': None,
        }

        # Read the partition's boot block at partition_start + 0xC00
        bb_offset = start_byte + FILECORE_BOOT_BLOCK_OFFSET
        if bb_offset + FILECORE_BOOT_BLOCK_SIZE <= file_size:
            with open(input_path, 'rb') as f:
                f.seek(bb_offset)
                boot_block = f.read(FILECORE_BOOT_BLOCK_SIZE)

            if len(boot_block) == FILECORE_BOOT_BLOCK_SIZE:
                bb_checksum_ok = _validate_filecore_boot_block_checksum(boot_block)
                partition_info['boot_block_valid'] = bb_checksum_ok

                # Extract protection flags and password hashes
                partition_info['protection'] = _decode_ics_protection(
                    boot_block[ICS_PROTECTION_OFFSET]
                )
                partition_info['password_hash'] = _extract_ics_password_hashes(
                    boot_block
                )

                # Parse FileCore disc record for disc name and size
                disc_record = boot_block[FILECORE_BB_DISC_RECORD_OFFSET:]
                if bb_checksum_ok and _is_valid_filecore_disc_record(disc_record):
                    dr = _parse_filecore_disc_record(disc_record)
                    partition_info['disc_name'] = dr['disc_name']
                elif _is_valid_filecore_disc_record_strict(boot_block):
                    dr = _parse_filecore_disc_record(disc_record)
                    partition_info['disc_name'] = dr['disc_name']
                    log.warning(
                        f"ICS IDEFS partition {valid_count}: boot block "
                        f"checksum invalid but disc record looks valid"
                    )

        partitions.append(partition_info)
        valid_count += 1

    return {
        'detected': len(partitions) > 0,
        'scheme': 'ics_idefs',
        'total_capacity_sectors': total_capacity_sectors,
        'total_capacity_bytes': total_capacity_sectors * ICS_SECTOR_SIZE,
        'partitions': partitions,
    }


# =========================================================================
# HCCS partition detection
# =========================================================================

def _decode_hccs_password(obfuscated: bytes) -> str:
    """Decode an HCCS XOR-obfuscated partition password."""
    xor_key = bytes([0x06, 0x14, 0x1F, 0x07, 0x02, 0x1D, 0x17, 0x17])
    decoded = bytes(b ^ k for b, k in zip(obfuscated[:8], xor_key))
    return decoded.rstrip(b'\x00').decode('latin-1', errors='replace')

def _decode_hccs_access_flags(flags: int) -> dict:
    """
    Decode an HCCS / Simtec access-flags word.

    Returns a dict with boolean ``read``, ``write``, and ``not_mounted``
    fields plus a human-readable ``summary`` string.
    """
    read = bool(flags & 0x10)
    write = bool(flags & 0x20)
    not_mounted = bool(flags & 0x200)

    if read and write:
        summary = 'read/write'
    elif read:
        summary = 'read only'
    else:
        summary = 'no access'
    if not_mounted:
        summary += ', not mounted'

    return {
        'read': read,
        'write': write,
        'not_mounted': not_mounted,
        'raw': f'0x{flags:04X}',
        'summary': summary,
    }

# =========================================================================
# ICS / Baildon Electronics partition helpers
# =========================================================================

def _validate_ics_checksum(sector: bytes) -> bool:
    """Validate the ICS IDEFS partition table checksum.

    The checksum is: seed 0x50617274 ("Part") plus the sum of the first
    508 bytes (offsets 0x000–0x1FB), compared against the uint32le at
    offset 0x1FC.
    """
    if len(sector) < ICS_PARTITION_TABLE_SIZE:
        return False
    expected = struct.unpack_from('<I', sector, ICS_CHECKSUM_OFFSET)[0]
    checksum = ICS_CHECKSUM_SEED
    for i in range(ICS_CHECKSUM_OFFSET):
        checksum += sector[i]
    checksum &= 0xFFFFFFFF
    return checksum == expected


def _decode_ics_protection(flags_byte: int) -> dict:
    """Decode the ICS IDEFS protection flags byte (boot block offset 0x1A7).

    Bits 1:0 encode the protection level:
      0 = no protection
      1 = read/write access requires password
      2 = read-only access requires password
      3 = no access (fully locked)
    """
    level = flags_byte & 0x03
    summaries = {
        0: 'none',
        1: 'read/write (password required)',
        2: 'read only (password required)',
        3: 'no access',
    }
    return {
        'level': level,
        'summary': summaries[level],
        'raw': f'0x{flags_byte:02X}',
    }


def _extract_ics_password_hashes(boot_block: bytes) -> dict:
    """Extract ICS IDEFS password hash words from a boot block.

    The hash is a non-reversible shift-XOR with key 0x01810284.
    We store the two uint32le words at offsets 0x1A8 (lo) and 0x1AC (hi)
    for display only.
    """
    hash_lo = struct.unpack_from('<I', boot_block, ICS_PASSWORD_HASH_OFFSET)[0]
    hash_hi = struct.unpack_from('<I', boot_block, ICS_PASSWORD_HASH_OFFSET + 4)[0]
    return {
        'hash_lo': f'0x{hash_lo:08X}',
        'hash_hi': f'0x{hash_hi:08X}',
    }


def _detect_hccs_partitions(input_path: Path) -> dict:
    """
    Detect HCCS-format Acorn partitions.

    HCCS partitions follow each other contiguously.  The first partition
    starts at byte 0; its boot block (at partition_start + 0xC00) contains
    an ``Andy`` magic at +0x1B0 followed by the password, access flags
    and a Filecore disc record whose ``disc_size`` field gives the
    partition length in bytes.  The next partition starts immediately
    after, and so on.

    Returns:
        Dict with ``detected``, ``scheme``, ``partitions`` list.
    """
    file_size = input_path.stat().st_size
    partitions = []
    current_offset = 0
    index = 0

    with open(input_path, 'rb') as f:
        while current_offset + FILECORE_BOOT_BLOCK_OFFSET + FILECORE_BOOT_BLOCK_SIZE <= file_size:
            # Read boot block (512 bytes at 0xC00 from partition start)
            f.seek(current_offset + FILECORE_BOOT_BLOCK_OFFSET)
            boot_block = f.read(FILECORE_BOOT_BLOCK_SIZE)
            if len(boot_block) < FILECORE_BOOT_BLOCK_SIZE:
                break

            # Check magic
            magic = boot_block[FILECORE_BB_HWDEP_OFFSET:FILECORE_BB_HWDEP_OFFSET + 4]
            if magic != b'Andy':
                break

            # --- Partition header (at 0x1B0) ---
            hdr_off = FILECORE_BB_HWDEP_OFFSET
            password = _decode_hccs_password(boot_block[hdr_off + 4:hdr_off + 12])
            access_before = struct.unpack_from('<H', boot_block, hdr_off + 12)[0]
            access_after = struct.unpack_from('<H', boot_block, hdr_off + 14)[0]

            # --- Disc record (at 0x1C0) ---
            disc_record = boot_block[FILECORE_BB_DISC_RECORD_OFFSET:]
            dr = _parse_filecore_disc_record(disc_record)
            partition_size = dr['disc_size']
            if partition_size == 0:
                log.warning(f"HCCS partition {index}: disc record has zero size, stopping")
                break

            # Clamp to remaining image
            if current_offset + partition_size > file_size:
                partition_size = file_size - current_offset

            partitions.append({
                'index': index,
                'start_byte': current_offset,
                'size_bytes': partition_size,
                'filesystem': 'adfs',
                'scheme': 'hccs',
                'description': f'HCCS partition {index}',
                'disc_name': dr['disc_name'],
                'password': password,
                'access_default': _decode_hccs_access_flags(access_before),
                'access_unlocked': _decode_hccs_access_flags(access_after),
            })

            current_offset += partition_size
            index += 1

    return {
        'detected': len(partitions) > 0,
        'scheme': 'hccs',
        'partitions': partitions,
    }


# =========================================================================
# SJ Research Nexus Disc Sharer partition detection
# =========================================================================

def _detect_nexus_partitions(input_path: Path) -> dict:
    """
    Detect an SJ Research Nexus Disc Sharer partition table.

    The table sits at a fixed offset (NEXUS_TABLE_OFFSET = 0x20000,
    i.e. 256 × 512-byte sectors from the start of the disc) and is
    identified by the four-byte magic ``Net1``.  The 0x20000 bytes before
    the table contain the disc sharer firmware; this region is captured in
    the returned dict so that the caller can label it appropriately when
    carving unpartitioned space.

    Partition addresses are stored as plain sector numbers (ui32le).
    The combined size/drive word stores the drive number in bits 31–24
    and the size in sectors in bits 23–0.

    Printer partitions (flag bit 3) are checked against the Filecore boot
    block checksum as a quick ADFS sanity test.  If the check passes the
    filesystem is set to ``adfs``; if it fails an informational message is
    logged and the filesystem is still set to NEXUS_PRINTER_FILESYSTEM
    (default ``'adfs'``).  Set NEXUS_PRINTER_FILESYSTEM to a different
    string to override the label, or to None to omit printer partitions
    from the output entirely (they will appear as unpartitioned gaps).

    Disc names are read from the Filecore disc record inside the boot block
    at ``partition_start + 0xC00`` when the boot block is present.

    Returns:
        Dict with ``detected`` (bool).  When True, also includes
        ``scheme`` (``'nexus'``), ``partitions`` (list of dicts with at
        least ``index``, ``start_byte``, ``size_bytes``, ``filesystem``),
        and ``nexus_header`` with disc-level Nexus metadata.
    """
    file_size = input_path.stat().st_size

    # Minimum size: table offset + header + at least one entry
    if file_size < NEXUS_TABLE_OFFSET + NEXUS_TABLE_HEADER_SIZE + NEXUS_TABLE_ENTRY_SIZE:
        return {'detected': False, 'scheme': 'nexus'}

    partitions = []
    network_number = header_unknown_5 = delay = 0

    with open(input_path, 'rb') as f:
        f.seek(NEXUS_TABLE_OFFSET)
        table_bytes = f.read(256)

        if len(table_bytes) < NEXUS_TABLE_HEADER_SIZE or table_bytes[:4] != NEXUS_TABLE_MAGIC:
            return {'detected': False, 'scheme': 'nexus'}

        # --- Partition table header ---
        network_number    = table_bytes[4]
        header_unknown_5  = table_bytes[5]
        delay             = (table_bytes[7] << 8) | table_bytes[6]  # delay_high, delay_low

        for i in range(NEXUS_TABLE_MAX_ENTRIES):
            eo = NEXUS_TABLE_HEADER_SIZE + i * NEXUS_TABLE_ENTRY_SIZE
            if eo + NEXUS_TABLE_ENTRY_SIZE > len(table_bytes):
                break

            flags_byte      = table_bytes[eo]
            station         = table_bytes[eo + 1]
            # eo+2, eo+3 are reserved
            addr_word       = struct.unpack_from('<I', table_bytes, eo + 4)[0]
            size_drive_word = struct.unpack_from('<I', table_bytes, eo + 8)[0]
            # eo+12..eo+15 are reserved

            decoded_flags = _decode_nexus_flags(flags_byte)
            is_last    = decoded_flags['last']
            is_printer = decoded_flags['printer']

            start_sector    = addr_word
            drive_number    = (size_drive_word >> 24) & 0xFF
            size_in_sectors = size_drive_word & 0x00FFFFFF
            start_byte_val  = start_sector    * NEXUS_SECTOR_SIZE
            size_bytes_val  = size_in_sectors * NEXUS_SECTOR_SIZE

            if size_bytes_val == 0:
                if is_last:
                    break
                continue

            # Respect NEXUS_PRINTER_FILESYSTEM = None to omit printer partitions
            if is_printer and NEXUS_PRINTER_FILESYSTEM is None:
                log.debug(f"Nexus partition {i}: printer partition omitted (NEXUS_PRINTER_FILESYSTEM=None)")
                if is_last:
                    break
                continue

            # Clamp to image size
            if start_byte_val + size_bytes_val > file_size:
                size_bytes_val = max(0, file_size - start_byte_val)

            # --- Filecore boot block: read once, used for ADFS check and disc name ---
            bb_abs     = start_byte_val + FILECORE_BOOT_BLOCK_OFFSET
            boot_block = b''
            if bb_abs + FILECORE_BOOT_BLOCK_SIZE <= file_size:
                f.seek(bb_abs)
                boot_block = f.read(FILECORE_BOOT_BLOCK_SIZE)

            # --- Determine filesystem ---
            if is_printer:
                # Printer partitions are not Filecore formatted; they hold
                # print-spool data.  Mark them with NEXUS_PRINTER_FILESYSTEM
                # ('other' by default) so they are registered as downloadable
                # artefacts without being fed into the ADFS extraction pipeline.
                filesystem = NEXUS_PRINTER_FILESYSTEM
            else:
                filesystem = 'adfs'

            # --- Parse disc name from Filecore disc record ---
            # Only extract the disc name when the boot block has a valid ADFS
            # checksum.  If the checksum fails the boot block is either absent
            # or contains garbage (e.g. because the Nexus partition table's
            # sector addresses placed this partition at the wrong location on
            # disc), and decoding the name field from garbage bytes can produce
            # strings with embedded NUL characters that PostgreSQL rejects.
            disc_name = ''
            if len(boot_block) >= FILECORE_BOOT_BLOCK_SIZE:
                bb_checksum_ok = _validate_filecore_boot_block_checksum(boot_block)
                if bb_checksum_ok:
                    dr = _parse_filecore_disc_record(boot_block[FILECORE_BB_DISC_RECORD_OFFSET:])
                    disc_name = dr.get('disc_name', '')
                else:
                    log.warning(
                        f"Nexus partition {i}: boot block checksum invalid"
                        f" — disc name not extracted (partition boundaries may be incorrect)"
                    )

            partition = {
                'index':                  i,
                'start_byte':             start_byte_val,
                'size_bytes':             size_bytes_val,
                'filesystem':             filesystem,
                'scheme':                 'nexus',
                'description':            f'Nexus partition {i}',
                'nexus_drive_number':     drive_number,
                'nexus_station':          station,
                'nexus_flags':            decoded_flags,
                'nexus_network_number':   network_number,
                'nexus_header_unknown_5': header_unknown_5,
                'nexus_delay':            delay,
            }
            if disc_name:
                partition['disc_name'] = disc_name

            partitions.append(partition)

            if is_last:
                break

    if not partitions:
        # Magic matched but no usable entries — treat as not detected
        return {'detected': False, 'scheme': 'nexus'}

    return {
        'detected': True,
        'scheme': 'nexus',
        'partitions': partitions,
        'nexus_header': {
            'nexus_network_number':   network_number,
            'nexus_header_unknown_5': header_unknown_5,
            'nexus_delay':            delay,
        },
    }


# =========================================================================
# Simtec signature detection (decode not implemented)
# =========================================================================

def _detect_simtec_signature(input_path: Path) -> dict:
    """
    Detect the Simtec IDEFS partition table signature.

    Only the presence of the ``andy`` magic at 0xC00 + 0x1B0 is checked;
    the partition table is not decoded because the documentation is
    incomplete (password algorithm and some fields are unknown).

    Returns:
        Dict with ``detected``, ``scheme``, and a ``description``.
    """
    file_size = input_path.stat().st_size
    if file_size < FILECORE_BOOT_BLOCK_OFFSET + FILECORE_BOOT_BLOCK_SIZE:
        return {'detected': False, 'scheme': 'simtec'}

    with open(input_path, 'rb') as f:
        f.seek(FILECORE_BOOT_BLOCK_OFFSET + FILECORE_BB_HWDEP_OFFSET)
        magic = f.read(4)

    if magic != b'andy':
        return {'detected': False, 'scheme': 'simtec'}

    return {
        'detected': True,
        'scheme': 'simtec',
        'partitions': [],  # No partition decoding for Simtec
        'description': (
            'Simtec IDEFS partition table detected '
            '(partition decoding not implemented — documentation incomplete)'
        ),
    }


# =========================================================================
# Top-level Acorn partition scheme dispatcher
# =========================================================================

# Priority-ordered registry of Acorn partition scheme probes.
# Each entry is (scheme_name, probe_fn).  To add a new scheme, write a
# _detect_<scheme> function and append (or insert) it here.
#
# Priority rationale:
#  1. nexus  — 'Net1' magic at 0x20000 (unambiguous, fast; checked first so
#              HCCS doesn't waste time on Nexus discs whose 0xC00 area holds
#              sharer firmware rather than the 'Andy' Filecore boot block)
#  2. ics    — strong "Part" checksum at sector 0 gives unambiguous ID
#              without touching the 0xC00 area, so checked before HCCS
#  3. hccs   — most common Acorn hard-disc partitioning
#  4. simtec — signature only; documentation incomplete
_ACORN_SCHEMES = [
    ('nexus',  _detect_nexus_partitions),   # SJ Research Nexus Disc Sharer
    ('ics',    _detect_ics_partitions),     # ICS / Baildon Electronics IDEFS
    ('hccs',   _detect_hccs_partitions),    # HCCS
    ('simtec', _detect_simtec_signature),   # Simtec IDEFS
    # (Future: RISC iX, etc.)
]


def detect_acorn_partitions(input_path: Path) -> dict:
    """
    Try all known Acorn partitioning schemes in priority order.

    Each scheme detector returns partitions with byte-based offsets
    (``start_byte`` / ``size_bytes``) so the caller can use them
    directly for image carving and gap detection.

    To add a new scheme, write a ``_detect_<scheme>`` function and
    add it to ``_ACORN_SCHEMES`` in the appropriate priority position.

    Returns:
        A dict with at least ``detected`` (bool).  When a scheme is
        found, also includes ``scheme`` (str) and ``partitions`` (list).
        Schemes that are detected but cannot be decoded (e.g. Simtec)
        return ``partitions: []`` with a ``description``.
    """
    for _name, probe in _ACORN_SCHEMES:
        result = probe(input_path)
        if result.get('detected'):
            return result
    return {'detected': False}


# =========================================================================
# Standard (PC) partition detection — sfdisk
# =========================================================================


def detect_partitions_sfdisk(input_path: Path) -> dict:
    """
    Detect partitions using sfdisk (handles MBR and GPT).

    Args:
        input_path: Path to raw disc image

    Returns:
        Result dict with success status, partitions list, and process_output
    """
    try:
        cmd = ['sfdisk', '--json', str(input_path)]
        result, process_output = run_tool_with_output(cmd, timeout=30)
    except FileNotFoundError:
        return tool_result(
            False, tool='sfdisk', error='sfdisk not installed', partitions=[],
        )

    if result.returncode != 0:
        return tool_result(
            False, tool='sfdisk',
            error=f'sfdisk failed (exit {result.returncode})',
            process_output=process_output,
            partitions=[],
        )

    try:
        data = json.loads(result.stdout)
        table = data.get('partitiontable', {})
        sector_size = table.get('sectorsize', 512)
        table_type = table.get('label', 'unknown')
        partitions = []

        for i, part in enumerate(table.get('partitions', [])):
            start_sector = part.get('start', 0)
            size_sectors = part.get('size', 0)
            partitions.append({
                'index': i,
                'start_sector': start_sector,
                'size_sectors': size_sectors,
                # Normalise to byte offsets for uniform downstream handling
                'start_byte': start_sector * sector_size,
                'size_bytes': size_sectors * sector_size,
                'type': part.get('type', 'unknown'),
                'node': part.get('node', ''),
            })

        # Check for an empty DOS/MBR partition table: the 55 AA signature
        # is present but all partition entries have type 0 and/or size 0.
        # This happens on discs reformatted from PC to another format
        # (e.g. Acorn) that retain a stale MBR in the first sector.
        # Log the situation and report no usable partitions so the caller
        # can fall through to other detection methods.
        if table_type == 'dos' and partitions:
            non_empty = [p for p in partitions if p['type'] != '0' and p['size_bytes'] > 0]
            if not non_empty:
                log.info(
                    f"sfdisk found a DOS partition table but all {len(partitions)} "
                    f"entry/entries are empty (type 0 / size 0) — likely a stale MBR"
                )
                partitions = []

        warnings = []
        dropped_partitions = []

        # PRIMARY CHECK: Does the boot sector look like a FAT BPB?
        # A DOS floppy/volume starts with a FAT BPB, not an MBR.  The bytes
        # sfdisk interprets as the partition table (0x1BE–0x1FD) are actually
        # BPB / bootstrap data, producing bogus entries with absurd sector
        # numbers.  If the BPB fields all validate, discard every sfdisk
        # partition and let the caller fall through to unpartitioned handling.
        if table_type == 'dos' and partitions:
            if detect_fat_filesystem(input_path) is not None:
                msg = (
                    "sfdisk: DOS partition table rejected — boot sector "
                    "contains a valid FAT BPB (unpartitioned FAT volume)"
                )
                log.info(msg)
                warnings.append(msg)
                dropped_partitions = partitions
                partitions = []

        # SECONDARY CHECK: discard partitions that start beyond the image.
        # A partition whose start offset is >= file_size does not exist in
        # the image at all and must be dropped.  A partition that starts
        # within the image but ends beyond it belongs to a truncated image
        # (e.g. bad sectors at the end of a disc); it is kept so that the
        # downstream extraction can recover whatever data is present.
        if partitions:
            file_size = input_path.stat().st_size
            in_range = []
            for p in partitions:
                if p['start_byte'] >= file_size:
                    msg = (
                        f"sfdisk: dropped partition {p['index']} — "
                        f"start {p['start_byte']} >= image size {file_size}"
                    )
                    log.info(msg)
                    warnings.append(msg)
                    dropped_partitions.append(p)
                else:
                    in_range.append(p)
            partitions = in_range

        # TERTIARY CHECK: reject overlapping partitions.
        # Two partitions whose byte ranges overlap cannot both be valid;
        # this indicates a corrupt or misinterpreted partition table.
        # Reject the entire table so the caller can fall through to other
        # detection methods rather than extracting nonsensical ranges.
        if len(partitions) > 1:
            sorted_parts = sorted(partitions, key=lambda p: p['start_byte'])
            for a, b in zip(sorted_parts, sorted_parts[1:]):
                a_end = a['start_byte'] + a['size_bytes']
                if a_end > b['start_byte']:
                    msg = (
                        f"sfdisk: partitions {a['index']} and {b['index']} overlap "
                        f"(end {a_end} > start {b['start_byte']}) — "
                        "rejecting partition table"
                    )
                    log.warning(msg)
                    warnings.append(msg)
                    dropped_partitions.extend(partitions)
                    partitions = []
                    break

        result_dict = {
            'success': len(partitions) > 0,
            'tool': 'sfdisk',
            'table_type': table_type,
            'sector_size': sector_size,
            'partitions': partitions,
            'process_output': process_output,
        }
        if warnings:
            result_dict['warnings'] = warnings
        if dropped_partitions:
            result_dict['dropped_partitions'] = dropped_partitions
        return result_dict
    except (json.JSONDecodeError, KeyError) as e:
        return tool_result(
            False, tool='sfdisk',
            error=f'Failed to parse sfdisk output: {e}',
            process_output=process_output,
            partitions=[],
        )


# =========================================================================
# ADFS filesystem signature detection
# =========================================================================

def _determine_adfs_subformat(
    adfs_variant: str | None,
    boot_block_at: str | None,
    disc_size: int,
    header: bytes,
    sbpr_offset: int | None,
) -> str | None:
    """
    Determine the specific ADFS subformat (S, M, L, D, E, E+, F, F+).

    Old-format (Hugo directories) variants are distinguished by disc size:
      S: 40 tracks × 16 sectors × 256 B × 1 side = 163,840 B
      M: 80 tracks × 16 sectors × 256 B × 1 side = 327,680 B
      L: 80 tracks × 16 sectors × 256 B × 2 sides = 655,360 B
      D: 80 tracks × 5 sectors × 1024 B × 2 sides = 819,200 B

    New-format (SBPr directories) variants are distinguished by boot block
    location (floppy vs hard disc) and the "big directory" flag:
      E / E+: boot block at sector 0 (floppy)
      F / F+: boot block at disc address &C00 (hard disc)

    E+ and F+ use 4 KB "big directories".  They are identified by byte 5 of
    the SBPr directory header being 0xFF (IsNewBigDir flag).

    Returns one of 'S', 'M', 'L', 'D', 'E', 'E+', 'F', 'F+', or None.
    """
    if adfs_variant == 'old_map':
        # Old-format ADFS: S, M, L, D — identified by disc size
        if disc_size <= 163840:
            return 'S'
        elif disc_size <= 327680:
            return 'M'
        elif disc_size <= 655360:
            return 'L'
        else:
            return 'D'

    elif adfs_variant == 'new_map':
        # New-format ADFS: E, E+, F, F+
        # Check for big directories (E+ / F+): byte 5 of the SBPr header == 0xFF
        is_plus = (
            sbpr_offset is not None
            and sbpr_offset + 6 <= len(header)
            and header[sbpr_offset + 5] == 0xFF
        )
        if boot_block_at == 'C00':
            return 'F+' if is_plus else 'F'
        else:
            # boot_block_at == 'sector0' or None (directory-only detection)
            return 'E+' if is_plus else 'E'

    return None


def detect_acorn_adfs(input_path: Path) -> dict:
    """
    Detect Acorn ADFS filesystem by checking for known signatures.

    Checks for:
    - ADFS boot block checksum (sum of first 512 bytes == 0 mod 256)
    - ADFS disc record heuristics at &C00 (when checksum fails but disc
      record fields pass stricter validation)
    - "Hugo" signature (old-format ADFS directories: ADFS-S, M, L, D)
    - "SBPr" / "Nick" signatures (new-format ADFS directories: ADFS-E, E+, F, F+)

    A valid FAT BPB at sector 0 takes priority over all ADFS signatures.
    The ADFS checksum is only a 1-in-256 coincidence for any 512-byte block,
    while a matching FAT BPB requires several independent fields to be in
    range simultaneously.  Returning early here prevents DOS FAT discs whose
    sector-6 data happens to pass the Filecore checksum from being
    misidentified as ADFS.

    Args:
        input_path: Path to raw disc image

    Returns:
        Result dict with detection info including ``adfs_subformat`` ('S', 'M',
        'L', 'D', 'E', 'E+', 'F', 'F+') when determinable.
    """
    try:
        file_size = input_path.stat().st_size
        # Read first 16KB - enough to cover boot block and start of root directory
        read_size = min(file_size, 16384)

        with open(input_path, 'rb') as f:
            header = f.read(read_size)

        signatures = []
        adfs_variant = None
        # Track boot block locations independently; &C00 takes priority over
        # sector 0 when determining floppy vs hard-disc subformat because the
        # sector-0 checksum has a 1-in-256 false-positive rate whereas the
        # disc-record validation at &C00 is much stronger.
        boot_block_sector0 = False
        boot_block_C00 = False
        sbpr_offset = None  # offset of first SBPr directory header found

        # Check ADFS boot block checksum at sector 0 (floppy new-map formats:
        # D, E, E+).  Sector 0 on a floppy holds the zone-0 map block; the
        # disc record is at byte 4 of that block.
        #
        # Skip this check when sector 0 contains a FAT BPB: both structures
        # occupy the same physical sector so they are mutually exclusive, and
        # a real FAT BPB is a much stronger identifier than an 8-bit checksum.
        # (The 0xC00 check below is for a different sector and is unaffected.)
        if len(header) >= 512 and detect_fat_filesystem(header[:512]) is None:
            if sum(header[0:512]) & 0xFF == 0:
                if _is_valid_filecore_disc_record(header[4:]):
                    signatures.append('Valid ADFS boot block checksum (sector 0)')
                    adfs_variant = 'new_map'
                    boot_block_sector0 = True

        # Check ADFS boot block checksum at disc address 0xC00 (hard-disc
        # new-map formats: F, F+).  The disc record lives at +0x1C0 within
        # this 512-byte boot block.
        #
        # No FAT BPB guard here: the FAT BPB is always in sector 0, which is
        # a completely different sector from 0xC00.  A disc reformatted from
        # FAT to ADFS may still carry the old BPB in sector 0 while having a
        # genuine Filecore boot block here.  Validate the disc record instead
        # of relying on the checksum alone to avoid coincidental matches.
        if len(header) >= 0xC00 + 512:
            boot_block = header[0xC00:0xC00 + 512]
            if sum(boot_block) & 0xFF == 0:
                if _is_valid_filecore_disc_record(boot_block[FILECORE_BB_DISC_RECORD_OFFSET:]):
                    signatures.append('Valid ADFS boot block checksum (disc address &C00)')
                    adfs_variant = 'new_map'
                    boot_block_C00 = True
            elif _is_valid_filecore_disc_record_strict(boot_block):
                # Checksum failed but the disc record fields are
                # individually valid.  Some ADFS formatters or disc
                # utilities produce boot blocks with incorrect checksums;
                # accept them via stricter disc record validation.
                signatures.append('Valid ADFS disc record without checksum (disc address &C00)')
                adfs_variant = 'new_map'
                boot_block_C00 = True

        # Check for old-format directory signature "Hugo".
        # "Hugo" appears at byte 1 of the directory header (after the master
        # sequence number) and again near the end of the directory tail.
        #
        # Root directory location depends on sector size:
        #   - 256-byte sectors (floppy S/M/L/D): root at 0x200
        #       header Hugo: 0x201
        #       tail Hugo (1280-byte dir): 0x6CB  (0x200 + 0x4CB)
        #       tail Hugo (2048-byte dir): 0x9FB  (0x200 + 0x7FB)
        #   - 512-byte sectors (hard disc, D+ variants): root at 0x400
        #       header Hugo: 0x401
        #       tail Hugo (1280-byte dir): 0x8CB  (0x400 + 0x4CB)
        #       tail Hugo (2048-byte dir): 0xBFB  (0x400 + 0x7FB)
        #
        # 0x4CB is also checked for images where the directory starts at byte 0
        # (non-standard but seen in some extracted/synthetic images).
        for offset in [0x201, 0x401, 0x4CB, 0x6CB, 0x8CB, 0x9FB, 0xBFB]:
            if offset + 4 <= len(header) and header[offset:offset + 4] == b"Hugo":
                signatures.append(f'"Hugo" at 0x{offset:X} (old-format directory)')
                adfs_variant = 'old_map'

        # Check for new-format directory header "SBPr"
        # Scan sector-aligned offsets in the first 16KB
        for offset in range(0, len(header) - 4, 512):
            if header[offset:offset + 4] == b"SBPr":
                signatures.append(f'"SBPr" at 0x{offset:X} (new-format directory)')
                if not adfs_variant:
                    adfs_variant = 'new_map'
                if sbpr_offset is None:
                    sbpr_offset = offset
                break

        # Check for new-format directory tail "Nick".
        # Offsets mirror the Hugo tail offsets above, covering both 256-byte
        # and 512-byte sector layouts.
        for offset in [0x4CB, 0x6CB, 0x7FF, 0x8CB, 0x9FB, 0xBFF, 0xBFB]:
            if offset + 4 <= len(header) and header[offset:offset + 4] == b"Nick":
                signatures.append(f'"Nick" at 0x{offset:X} (new-format directory tail)')
                if not adfs_variant:
                    adfs_variant = 'new_map'

        # &C00 boot block takes priority over sector-0 boot block for
        # floppy/hard-disc subformat classification.
        boot_block_at = 'C00' if boot_block_C00 else ('sector0' if boot_block_sector0 else None)

        adfs_subformat = _determine_adfs_subformat(
            adfs_variant, boot_block_at, file_size, header, sbpr_offset,
        ) if adfs_variant else None

        return {
            'adfs_detected': len(signatures) > 0,
            'adfs_variant': adfs_variant,
            'adfs_subformat': adfs_subformat,
            'boot_block_at': boot_block_at,
            'disc_size': file_size,
            'signatures': signatures,
        }

    except Exception as e:
        return {
            'adfs_detected': False,
            'error': str(e),
        }


# =========================================================================
# Generic format identification
# =========================================================================

def detect_format_file_cmd(input_path: Path) -> dict:
    """
    Use the 'file' command to identify disc image format.

    Args:
        input_path: Path to disc image

    Returns:
        Result dict with file type info and process_output
    """
    try:
        cmd = ['file', '-b', str(input_path)]
        result, process_output = run_tool_with_output(cmd, timeout=10)
    except FileNotFoundError:
        return tool_result(
            False, tool='file', error='file command not installed', file_type='',
        )

    file_type = result.stdout.decode(errors='replace').strip() if result.returncode == 0 else ''

    return tool_result(
        result.returncode == 0,
        tool='file',
        process_output=process_output,
        file_type=file_type,
    )



# =========================================================================
# FAT filesystem identification (boot sector BPB)
# =========================================================================

# Valid bytes-per-sector values (powers of 2 from 512 to 4096)
_FAT_VALID_BPS = frozenset({512, 1024, 2048, 4096})

# Valid media-descriptor byte values (FAT spec §3)
_FAT_VALID_MEDIA = frozenset({
    0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF,
})


def detect_fat_filesystem(source: bytes | Path) -> str | None:
    """
    Identify a FAT12/16/32 filesystem from a raw disc image.

    *source* may be either a :class:`~pathlib.Path` (the first 512 bytes are
    read from the file) or a :class:`bytes` object of at least 512 bytes
    (the caller's buffer is used directly, avoiding a second file read when
    the sector is already in memory).

    Returns ``'fat12'``, ``'fat16'`` or ``'fat32'``, or ``None`` if the image
    does not look like a FAT volume.

    Pure Python — reads exactly 512 bytes, no subprocesses.  The function is
    intentionally conservative: every checked BPB field must be within its
    legal range so that Acorn, ISO and other formats are never misclassified.

    References:
      Microsoft "FAT: General Overview of On-Disk Format" v1.03 (Dec 2000)
      https://download.microsoft.com/download/1/6/1/161ba512-40e2-4cc9-843a-923143f3456c/fatgen103.doc

    FAT type is determined in order:
      1. Explicit type string at offset 54–61 (FAT12/16) or 82–89 (FAT32).
      2. Cluster-count method (spec §3.5) for images where the formatter did
         not write a type string.
    """
    if isinstance(source, bytes):
        sector = source
    else:
        try:
            with open(source, 'rb') as f:
                sector = f.read(512)
        except OSError:
            return None

    if len(sector) < 512:
        return None

    # ── Structural checks ────────────────────────────────────────────────

    # Boot-sector signature (0x55 0xAA at bytes 510–511).
    # Spec §3.1: "This signature is present in all FAT boot sectors."
    if sector[510] != 0x55 or sector[511] != 0xAA:
        return None

    # BPB_BytsPerSec (LE 16-bit, offset 11): 512 / 1024 / 2048 / 4096.
    # Spec §3.1: "Legal values for this field are 512, 1024, 2048, or 4096."
    # (Not relying on BS_jmpBoot / byte 0: the jump instruction is a PC BIOS
    # artefact and may differ or be absent in non-PC FAT images.)
    bps = int.from_bytes(sector[11:13], 'little')
    if bps not in _FAT_VALID_BPS:
        return None

    # BPB_SecPerClus (offset 13): must be a non-zero power of two ≤ 128.
    # Spec §3.1: "Legal values are 1, 2, 4, 8, 16, 32, 64, and 128."
    spc = sector[13]
    if spc == 0 or (spc & (spc - 1)) != 0 or spc > 128:
        return None

    # BPB_RsvdSecCnt (LE 16-bit, offset 14): at least 1.
    # Spec §3.1: "Must not be 0."
    rsvd = int.from_bytes(sector[14:16], 'little')
    if rsvd < 1:
        return None

    # BPB_NumFATs (offset 16): 1 or 2.
    # Spec §3.1: "Any FAT volume must have at least 1 FAT … strongly
    # recommend … always 2."
    num_fats = sector[16]
    if num_fats not in (1, 2):
        return None

    # BPB_Media (offset 21): 0xF0 or 0xF8–0xFF.
    # Spec §3.1: "Legal values for this field are 0xF0, 0xF8, 0xF9, 0xFA,
    # 0xFB, 0xFC, 0xFD, 0xFE, and 0xFF."
    if sector[21] not in _FAT_VALID_MEDIA:
        return None

    # ── FAT variant determination ─────────────────────────────────────────

    # BPB_FATSz16 (LE 16-bit, offset 22): zero only on FAT32
    fat_sz16 = int.from_bytes(sector[22:24], 'little')

    if fat_sz16 == 0:
        # FAT32: BS_FilSysType at offset 82–89
        fs_type = sector[82:90].rstrip(b' \x00')
        if fs_type == b'FAT32':
            return 'fat32'
        # No type string — trust the BPB structure (fat_sz16 == 0 implies FAT32)
        fat_sz32 = int.from_bytes(sector[36:40], 'little')
        if fat_sz32 == 0:
            return None  # Neither FAT size field set; reject
        return 'fat32'

    # FAT12 or FAT16: BS_FilSysType at offset 54–61
    fs_type = sector[54:62].rstrip(b' \x00')
    if fs_type == b'FAT12':
        return 'fat12'
    if fs_type == b'FAT16':
        return 'fat16'

    # No type string (common for pre-DOS 4.0 formatters) — use the cluster
    # count method from spec §3.5 to distinguish FAT12/16.
    root_ent_cnt  = int.from_bytes(sector[17:19], 'little')
    tot_sec16     = int.from_bytes(sector[19:21], 'little')
    tot_sec32     = int.from_bytes(sector[32:36], 'little')
    total_sectors = tot_sec16 if tot_sec16 != 0 else tot_sec32
    if total_sectors == 0:
        return None

    root_dir_sectors   = (root_ent_cnt * 32 + bps - 1) // bps
    data_sectors       = total_sectors - rsvd - (num_fats * fat_sz16) - root_dir_sectors
    count_of_clusters  = data_sectors // spc

    if count_of_clusters < 4085:
        return 'fat12'
    if count_of_clusters < 65525:
        return 'fat16'
    return 'fat32'  # fat_sz16 non-zero but cluster count says FAT32: unusual


# FAT directory entry constants (spec §6)
_FAT_ATTR_READ_ONLY = 0x01
_FAT_ATTR_HIDDEN    = 0x02
_FAT_ATTR_SYSTEM    = 0x04
_FAT_ATTR_VOLUME_ID = 0x08
_FAT_ATTR_DIRECTORY = 0x10
_FAT_ATTR_ARCHIVE   = 0x20
_FAT_ATTR_LONG_NAME = (
    _FAT_ATTR_READ_ONLY | _FAT_ATTR_HIDDEN
    | _FAT_ATTR_SYSTEM | _FAT_ATTR_VOLUME_ID
)  # 0x0F


def _decode_fat_label(raw: bytes) -> str | None:
    """Decode an 11-byte FAT volume label field.

    Labels are stored space-padded in the OEM codepage (typically CP437 or
    CP850).  "NO NAME    " is the sentinel for "no label set".  Returns the
    stripped Unicode string, or ``None`` if the label is empty or the
    "NO NAME" sentinel.
    """
    # Truncate at the first NUL byte (C-string semantics; NUL is not a valid
    # FAT label character and Linux NUL-terminates POSIX filenames).
    nul_pos = raw.find(b'\x00')
    stripped = raw[:nul_pos].rstrip(b' ') if nul_pos >= 0 else raw.rstrip(b' ')
    if not stripped:
        return None
    # DIR_Name byte 0 of 0x05 represents a legitimate 0xE5 first character
    # (Japanese Kanji), which would otherwise collide with the deleted-entry
    # marker.  Restore the original byte before decoding.
    if stripped[0] == 0x05:
        stripped = b'\xE5' + stripped[1:]
    try:
        label = stripped.decode('cp850').rstrip()
    except UnicodeDecodeError:
        label = stripped.decode('cp850', errors='replace').rstrip()
    if not label or label.upper() == 'NO NAME':
        return None
    return label


def read_fat_volume_label(path: Path) -> str | None:
    """Return the volume label of a FAT12/16/32 disc image, or ``None``.

    Prefers the authoritative root-directory ``ATTR_VOLUME_ID`` entry (which
    tools like DOS ``LABEL`` or Windows Explorer update).  Falls back to the
    BPB ``BS_VolLab`` field written at format time.

    Only valid FAT images are accepted: the caller should gate this function
    with :func:`detect_fat_filesystem` if the image type is uncertain.  The
    function tolerates truncated or unreadable images by returning ``None``
    rather than raising.

    Reference: Microsoft "FAT: General Overview of On-Disk Format" v1.03
    (Dec 2000), §3 (BPB), §6 (directory entries).
    """
    try:
        with open(path, 'rb') as f:
            sector = f.read(512)
            if len(sector) < 512 or sector[510] != 0x55 or sector[511] != 0xAA:
                return None

            bps          = int.from_bytes(sector[11:13], 'little')
            spc          = sector[13]
            rsvd         = int.from_bytes(sector[14:16], 'little')
            num_fats     = sector[16]
            root_ent_cnt = int.from_bytes(sector[17:19], 'little')
            fat_sz16     = int.from_bytes(sector[22:24], 'little')

            if bps not in _FAT_VALID_BPS or spc == 0 or num_fats not in (1, 2):
                return None

            if fat_sz16 == 0:
                # FAT32: root directory lives in a cluster chain starting at
                # BPB_RootClus (offset 44).  BS_VolLab is at offset 71.
                fat_sz32  = int.from_bytes(sector[36:40], 'little')
                root_clus = int.from_bytes(sector[44:48], 'little')
                bpb_label = sector[71:82]

                if fat_sz32 == 0 or root_clus < 2:
                    root_data = b''
                else:
                    first_data_sector = rsvd + (num_fats * fat_sz32)
                    root_dir_offset   = (
                        first_data_sector + (root_clus - 2) * spc
                    ) * bps
                    # Scan only the first cluster of the root directory.  The
                    # volume label, if present, is conventionally the first
                    # (or near-first) entry, and walking the full FAT32
                    # cluster chain requires parsing the FAT itself.
                    f.seek(root_dir_offset)
                    root_data = f.read(spc * bps)
            else:
                # FAT12/16: fixed-size root directory immediately after the
                # FATs.  BS_VolLab is at offset 43.
                bpb_label         = sector[43:54]
                first_root_sector = rsvd + (num_fats * fat_sz16)
                root_dir_offset   = first_root_sector * bps
                root_dir_size     = root_ent_cnt * 32
                if root_dir_size == 0:
                    root_data = b''
                else:
                    f.seek(root_dir_offset)
                    root_data = f.read(root_dir_size)

            for i in range(0, len(root_data), 32):
                entry = root_data[i:i + 32]
                if len(entry) < 32:
                    break
                first = entry[0]
                if first == 0x00:
                    break            # end-of-directory marker
                if first == 0xE5:
                    continue         # deleted entry
                attr = entry[11]
                if attr == _FAT_ATTR_LONG_NAME:
                    continue         # long filename fragment
                if (attr & (_FAT_ATTR_DIRECTORY | _FAT_ATTR_VOLUME_ID)
                        == _FAT_ATTR_VOLUME_ID):
                    label = _decode_fat_label(entry[0:11])
                    if label is not None:
                        return label

            return _decode_fat_label(bpb_label)
    except OSError:
        return None


# vim: ts=4 sw=4 et
