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
	hccs_magic = b'Andy'
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
			if magic != hccs_magic:
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
# Simtec signature detection (decode not implemented)
# =========================================================================

_SIMTEC_MAGIC = b'andy'


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

	if magic != _SIMTEC_MAGIC:
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
	# HCCS (most common Acorn hard-disc partitioning)
	result = _detect_hccs_partitions(input_path)
	if result['detected']:
		return result

	# Simtec IDEFS (signature only — documentation incomplete)
	result = _detect_simtec_signature(input_path)
	if result['detected']:
		return result

	# (Future: SJ Nexus, RISC iX, ICS, etc.)

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

        return {
            'success': len(partitions) > 0,
            'tool': 'sfdisk',
            'table_type': table.get('label', 'unknown'),
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

        # Check for old-format directory signature "Hugo"
        # Old-map root directory starts at byte 0x200 (sector 2 at 256 bytes/sector)
        # "Hugo" appears at end of directory: offset depends on directory size
        # Common offsets: 0x4CB (small dir), 0x6CB (large dir)
        for offset in [0x4CB, 0x6CB]:
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

        # Check for new-format directory tail "Nick"
        for offset in [0x4CB, 0x6CB, 0x7FF, 0xBFF]:
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

