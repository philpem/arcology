"""
Partition detection tools.

Tools for detecting partitions and filesystems in raw disc images.
Supports:
- sfdisk - Standard MBR/GPT partition tables
- Acorn partitioning schemes (HCCS, Simtec, and future additions)
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

from .base import run_tool_with_output
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
# Printer partitions are checked against the Filecore boot block checksum
# to confirm ADFS; this constant provides both the expected label and the
# fallback when that check fails.
# • 'adfs'  — assume ADFS (default; will also log if checksum fails)
# • None    — omit printer partitions from the partition list entirely
#             (they will appear as unpartitioned gaps in the output)
# • any str — use that string as the filesystem label without checking
NEXUS_PRINTER_FILESYSTEM = 'adfs'


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
				if NEXUS_PRINTER_FILESYSTEM == 'adfs':
					# Confirm ADFS by checking the Filecore boot block checksum.
					# Log a note when it fails so that the operator knows ADFS
					# is being assumed without confirmation.
					adfs_ok = (
						len(boot_block) == FILECORE_BOOT_BLOCK_SIZE
						and sum(boot_block) & 0xFF == 0
					)
					if not adfs_ok:
						log.info(
							f"Nexus partition {i} (printer): Filecore boot block "
							f"checksum not valid — boot block absent or partition "
							f"may not be ADFS (assuming '{NEXUS_PRINTER_FILESYSTEM}')"
						)
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
				bb_checksum_ok = (sum(boot_block) & 0xFF == 0)
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

def detect_acorn_partitions(input_path: Path) -> dict:
	"""
	Try all known Acorn partitioning schemes in priority order.

	Each scheme detector returns partitions with byte-based offsets
	(``start_byte`` / ``size_bytes``) so the caller can use them
	directly for image carving and gap detection.

	To add a new scheme, write a ``_detect_<scheme>`` function and
	call it here in the appropriate priority order.

	Returns:
		A dict with at least ``detected`` (bool).  When a scheme is
		found, also includes ``scheme`` (str) and ``partitions`` (list).
		Schemes that are detected but cannot be decoded (e.g. Simtec)
		return ``partitions: []`` with a ``description``.
	"""
	# SJ Research Nexus Disc Sharer (checked first: the 'Net1' magic is at
	# a fixed location 0x20000 bytes in, well away from the Filecore boot
	# block at 0xC00.  On a Nexus disc the 0xC00 area holds the disc
	# sharer firmware, so HCCS detection (which looks for 'Andy' there)
	# will naturally fail — but checking Nexus first is faster.)
	result = _detect_nexus_partitions(input_path)
	if result['detected']:
		return result

	# HCCS (most common Acorn hard-disc partitioning)
	result = _detect_hccs_partitions(input_path)
	if result['detected']:
		return result

	# Simtec IDEFS (signature only — documentation incomplete)
	result = _detect_simtec_signature(input_path)
	if result['detected']:
		return result

	# (Future: RISC iX, ICS, etc.)

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
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': 'sfdisk not installed',
        }

    if result.returncode != 0:
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': f'sfdisk failed (exit {result.returncode})',
            'process_output': process_output
        }

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

        # Validate partition offsets against the actual image file size.
        # A DOS floppy (e.g. FAT12/FAT16) has a valid 55 AA signature in
        # sector 0 that sfdisk interprets as an MBR, but the FAT BPB bytes
        # in the partition-table area (0x1BE-0x1FD) decode to absurdly large
        # sector numbers that start well beyond the image.  Discard any
        # partition whose start sector lies outside the image; if none remain,
        # report no usable partitions so the caller can fall through to other
        # detection methods (e.g. unpartitioned FAT detection).
        if partitions:
            file_size = input_path.stat().st_size
            in_range = [p for p in partitions if p['start_byte'] < file_size]
            if len(in_range) < len(partitions):
                n_dropped = len(partitions) - len(in_range)
                log.info(
                    f"sfdisk: dropped {n_dropped} partition(s) whose start offset "
                    f"exceeds image size ({file_size} bytes) — "
                    f"likely a DOS floppy misidentified as a partitioned disc"
                )
                partitions = in_range

        return {
            'success': len(partitions) > 0,
            'tool': 'sfdisk',
            'table_type': table_type,
            'sector_size': sector_size,
            'partitions': partitions,
            'process_output': process_output
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {
            'success': False,
            'tool': 'sfdisk',
            'partitions': [],
            'error': f'Failed to parse sfdisk output: {e}',
            'process_output': process_output
        }


# =========================================================================
# ADFS filesystem signature detection
# =========================================================================

def detect_acorn_adfs(input_path: Path) -> dict:
    """
    Detect Acorn ADFS filesystem by checking for known signatures.

    Checks for:
    - ADFS boot block checksum (sum of first 512 bytes == 0 mod 256)
    - "Hugo" signature (old-format ADFS directories: ADFS-S, M, L, D)
    - "SBPr" / "Nick" signatures (new-format ADFS directories: ADFS-E, E+, F, F+)

    Args:
        input_path: Path to raw disc image

    Returns:
        Result dict with detection info
    """
    try:
        file_size = input_path.stat().st_size
        # Read first 16KB - enough to cover boot block and start of root directory
        read_size = min(file_size, 16384)

        with open(input_path, 'rb') as f:
            header = f.read(read_size)

        signatures = []
        adfs_variant = None

        # Check ADFS boot block checksum (new-map: D, E, E+, F, F+)
        # The sum of all 512 bytes in the boot block should be 0 mod 256
        if len(header) >= 512:
            boot_checksum = sum(header[0:512]) & 0xFF
            if boot_checksum == 0:
                signatures.append('Valid ADFS boot block checksum (sector 0)')
                adfs_variant = 'new_map'

        # F-format (hard discs) has the block at disc address 0xC00
        if len(header) >= 0xC00+512:
            boot_checksum = sum(header[0xC00:0xC00+512]) & 0xFF
            if boot_checksum == 0:
                signatures.append('Valid ADFS boot block checksum (disc address &C00)')
                adfs_variant = 'new_map'

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
                break

        # Check for new-format directory tail "Nick".
        # Offsets mirror the Hugo tail offsets above, covering both 256-byte
        # and 512-byte sector layouts.
        for offset in [0x4CB, 0x6CB, 0x7FF, 0x8CB, 0x9FB, 0xBFF, 0xBFB]:
            if offset + 4 <= len(header) and header[offset:offset + 4] == b"Nick":
                signatures.append(f'"Nick" at 0x{offset:X} (new-format directory tail)')
                if not adfs_variant:
                    adfs_variant = 'new_map'

        return {
            'adfs_detected': len(signatures) > 0,
            'adfs_variant': adfs_variant,
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
        return {
            'success': False,
            'tool': 'file',
            'file_type': '',
            'error': 'file command not installed',
        }

    file_type = result.stdout.decode(errors='replace').strip() if result.returncode == 0 else ''

    return {
        'success': result.returncode == 0,
        'tool': 'file',
        'file_type': file_type,
        'process_output': process_output
    }

