"""
HFE format parser and mastering/protection analyser.

This module is a reusable library.  The low-level functions
(parse_hfe_header, get_track_bytes, walk_track) are public and intended
for future use by geometry detection and other analyses as well as the
current mastering detection.

HFE format reference
--------------------
Offset 0x000: PICFILEINFOV1 (512 bytes / block 0)
  uint8[8]  HEADERSIGNATURE   b'HXCPICFE' (v1/v2) or b'HXCHFEV3' (v3)
  uint8     formatrevision    0x00=v1 or v3, 0x01=v2
  uint8     number_of_track
  uint8     number_of_side
  uint8     track_encoding    0x00=ISOIBM_MFM, 0x01=Amiga_MFM,
                              0x02=ISOIBM_FM, 0xFF=Unknown
  uint16le  bitRate           kbps
  uint16le  floppyRPM
  uint8     floppyinterfacemode
  uint8     dnu               reserved
  uint16le  track_list_offset in 512-byte blocks (typically 1 → 0x200)

Track list (at block track_list_offset): array of PICTRACK
  uint16le  offset            track data start in 512-byte blocks
  uint16le  track_len         total bytes for both sides interleaved

Track data: 256-byte blocks, alternating sides
  bytes   0..255  = side 0
  bytes 256..511  = side 1  (repeats for remaining blocks)

Opcode schemes
--------------
v1 (HXCPICFE rev 0): no opcodes; 0xFF is plain data.
v2 (HXCPICFE rev 1): 0xFF is escape; next byte is opcode:
  0xF8 = RAND (weak bits) — followed by a length byte
  others — skip per spec
v3 (HXCHFEV3):  any byte with high nibble 0xF is an opcode:
  0xF0 = NOP
  0xF1 = SETINDEX
  0xF2 = SETBITRATE  — 1 payload byte
  0xF3 = SKIPBITS    — 1 payload byte
  0xF4 = RAND        — 1 payload byte (weak bits)
"""

import logging
import struct
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CRC-CCITT (IBM floppy variant): poly 0x1021, init 0xFFFF
# ---------------------------------------------------------------------------

def _crc16(data: bytes) -> int:
	crc = 0xFFFF
	for byte in data:
		crc ^= byte << 8
		for _ in range(8):
			if crc & 0x8000:
				crc = (crc << 1) ^ 0x1021
			else:
				crc <<= 1
			crc &= 0xFFFF
	return crc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_SECTOR_SIZES = (128, 256, 512, 1024, 2048, 4096, 8192)

# FM address mark raw 16-bit patterns (clock|data interleaved)
_FM_IDAM  = 0xF57E   # clock 0xC7, data 0xFE
_FM_DAM   = 0xF56F   # clock 0xC7, data 0xFB
_FM_DDAM  = 0xF56A   # clock 0xC7, data 0xF8

# MFM sync word
_MFM_SYNC = 0x4489   # A1 with missing clock


# ---------------------------------------------------------------------------
# Low-level: file structure
# ---------------------------------------------------------------------------

def parse_hfe_header(path: Path) -> dict:
	"""Parse HFE PICFILEINFOV1 header.

	Returns a dict with keys:
	  version          int | 'unknown'  (1, 2, 3 or 'unknown')
	  hfe_version_str  str              ('v1', 'v2', 'v3', 'unknown')
	  n_tracks         int
	  n_sides          int
	  track_encoding   str  ('mfm', 'fm', 'amiga_mfm', 'unknown')
	  bit_rate_kbps    int
	  floppy_rpm       int
	  interface_mode   int
	  track_list       list of {offset_blocks: int, len_bytes: int}

	Unrecognised signature: logged as a warning; parse attempted as v1.
	"""
	with open(path, 'rb') as f:
		header = f.read(512)

	if len(header) < 22:
		raise ValueError(f"HFE file too short: {path}")

	sig            = header[0:8]
	fmt_rev        = header[8]
	n_tracks       = header[9]
	n_sides        = header[10]
	track_enc_byte = header[11]
	bit_rate,      = struct.unpack_from('<H', header, 12)
	floppy_rpm,    = struct.unpack_from('<H', header, 14)
	iface_mode     = header[16]
	# byte 17 = dnu (reserved)
	tracklist_off, = struct.unpack_from('<H', header, 18)

	# Version detection
	if sig == b'HXCPICFE' and fmt_rev == 0x00:
		version, hfe_version_str = 1, 'v1'
	elif sig == b'HXCPICFE' and fmt_rev == 0x01:
		version, hfe_version_str = 2, 'v2'
	elif sig == b'HXCHFEV3':
		version, hfe_version_str = 3, 'v3'
	else:
		log.warning("HFE: unrecognised signature %r rev %02x in %s — attempting v1 parse", sig, fmt_rev, path)
		version, hfe_version_str = 'unknown', 'unknown'

	# Encoding
	enc_map = {0x00: 'mfm', 0x01: 'amiga_mfm', 0x02: 'fm', 0xFF: 'unknown'}
	track_encoding = enc_map.get(track_enc_byte, 'unknown')

	# Track list
	with open(path, 'rb') as f:
		f.seek(tracklist_off * 512)
		raw_tl = f.read(n_tracks * 4)

	track_list = []
	for i in range(n_tracks):
		off = i * 4
		if off + 4 > len(raw_tl):
			break
		t_offset, t_len = struct.unpack_from('<HH', raw_tl, off)
		track_list.append({'offset_blocks': t_offset, 'len_bytes': t_len})

	return {
		'version':         version,
		'hfe_version_str': hfe_version_str,
		'n_tracks':        n_tracks,
		'n_sides':         n_sides,
		'track_encoding':  track_encoding,
		'bit_rate_kbps':   bit_rate,
		'floppy_rpm':      floppy_rpm,
		'interface_mode':  iface_mode,
		'track_list':      track_list,
	}


def get_track_bytes(f: BinaryIO, track_entry: dict, side: int,
                    hfe_version: int = 1) -> tuple[bytes, list[int]]:
	"""De-interleave one side from an HFE track and strip opcodes.

	Track data is in 256-byte blocks; within each 512-byte block the first
	256 bytes are side 0 and the second 256 are side 1.

	Returns (track_bytes, weak_bit_offsets):
	  track_bytes       raw decoded bytes ready for walk_track()
	  weak_bit_offsets  list of byte offsets where weak-bit opcodes were found
	"""
	offset_bytes = track_entry['offset_blocks'] * 512
	total_bytes  = track_entry['len_bytes']

	f.seek(offset_bytes)
	raw = f.read(total_bytes)

	# De-interleave: pick the relevant 256-byte half of each 512-byte block
	side_bytes = bytearray()
	block_count = (len(raw) + 511) // 512
	for b in range(block_count):
		start = b * 512 + (side * 256)
		chunk = raw[start:start + 256]
		side_bytes.extend(chunk)

	# Strip opcodes and collect weak-bit positions
	clean = bytearray()
	weak_offsets: list[int] = []
	i = 0
	src = bytes(side_bytes)
	n = len(src)

	if hfe_version == 2:
		while i < n:
			b = src[i]
			if b == 0xFF and i + 1 < n:
				opcode = src[i + 1]
				if opcode == 0xF8:
					# RAND: escape(1) + opcode(1) + length(1)
					weak_offsets.append(len(clean))
					length = src[i + 2] if i + 2 < n else 0
					i += 3 + length
				else:
					# Other opcode: skip escape + opcode (payload varies by spec;
					# we skip just the two bytes as a safe minimum)
					i += 2
			else:
				clean.append(b)
				i += 1
	elif hfe_version == 3:
		while i < n:
			b = src[i]
			if (b & 0xF0) == 0xF0:
				opcode = b
				if opcode == 0xF0:    # NOP
					i += 1
				elif opcode == 0xF1:  # SETINDEX
					i += 1
				elif opcode == 0xF2:  # SETBITRATE
					i += 2
				elif opcode == 0xF3:  # SKIPBITS
					i += 2
				elif opcode == 0xF4:  # RAND (weak bits)
					weak_offsets.append(len(clean))
					i += 2
				else:
					i += 1
			else:
				clean.append(b)
				i += 1
	else:
		# v1 or unknown: no opcodes, pass through
		clean = bytearray(src)

	return bytes(clean), weak_offsets


# ---------------------------------------------------------------------------
# Mid-level: sector size search
# ---------------------------------------------------------------------------

def read_sector_search_size(
	track_bytes: bytes,
	dam_offset: int,
	declared_size: int,
	extra_sizes: tuple[int, ...] = _VALID_SECTOR_SIZES,
) -> dict:
	"""Read sector data at dam_offset, trying declared_size first.

	If the CRC at declared_size fails, retries each size in extra_sizes
	until a valid CRC is found (skipping declared_size to avoid double try).

	Returns:
	  data             bytes   sector payload (without the 2 CRC bytes)
	  size_used        int     size that was actually read
	  crc_valid        bool
	  declared_size    int     size from the IDAM N byte
	  size_was_overridden bool

	If no size yields a valid CRC, returns data at declared_size with
	crc_valid=False and size_was_overridden=False.

	Mirrors PC floppy controller behaviour: the host sets the DMA transfer
	count independently of the N byte in the IDAM.
	"""
	def _try(size: int) -> tuple[bytes, bool]:
		end = dam_offset + size + 2    # +2 for CRC bytes
		if end > len(track_bytes):
			return b'', False
		payload = track_bytes[dam_offset:dam_offset + size]
		crc_bytes = track_bytes[dam_offset + size:dam_offset + size + 2]
		stored_crc = struct.unpack('>H', crc_bytes)[0]
		# CRC covers the DAM byte (one byte before dam_offset) plus the data.
		# The caller passes dam_offset as the start of data, so we need the
		# DAM byte too.  We include it if available.
		if dam_offset > 0:
			crc_input = track_bytes[dam_offset - 1:dam_offset + size]
		else:
			crc_input = payload
		computed = _crc16(crc_input)
		return payload, (computed == stored_crc)

	payload, ok = _try(declared_size)
	if ok:
		return {
			'data': payload,
			'size_used': declared_size,
			'crc_valid': True,
			'declared_size': declared_size,
			'size_was_overridden': False,
		}

	for sz in extra_sizes:
		if sz == declared_size:
			continue
		payload2, ok2 = _try(sz)
		if ok2:
			return {
				'data': payload2,
				'size_used': sz,
				'crc_valid': True,
				'declared_size': declared_size,
				'size_was_overridden': True,
			}

	# Nothing worked — return declared_size data with crc_valid=False
	return {
		'data': payload,
		'size_used': declared_size,
		'crc_valid': False,
		'declared_size': declared_size,
		'size_was_overridden': False,
	}


# ---------------------------------------------------------------------------
# Mid-level: stream walkers
# ---------------------------------------------------------------------------

def _decode_fm_byte(raw16: int) -> int:
	"""Extract the 8 data bits from a 16-bit FM clock+data word."""
	data = 0
	for i in range(8):
		# Bit positions: clock at bit 15,13,11,... data at bit 14,12,10,...
		shift = 14 - (i * 2)
		data = (data << 1) | ((raw16 >> shift) & 1)
	return data


def _walk_fm_stream(track_bytes: bytes) -> list[dict]:
	"""Decode FM bitstream.

	FM represents each bit as two stream bits (clock + data).  Each decoded
	byte therefore occupies 2 raw bytes (16 bits).  Address marks are
	identified by special clock patterns; we scan for the known 16-bit
	patterns for IDAM, DAM, DDAM.

	Returns list of sector dicts:
	  cyl, head, sect, size_code, declared_size
	  dam_type   'DAM' | 'DDAM' | None
	  data       bytes (sector payload) or None if only IDAM seen
	  crc_valid  bool
	  size_used  int
	  size_was_overridden  bool
	  byte_offset_idam  int   (raw byte index in track_bytes)
	  byte_offset_dam   int or None
	"""
	sectors: list[dict] = []
	n = len(track_bytes)

	# We'll track IDAM info while looking for the following DAM
	pending_idam: dict | None = None

	i = 0
	while i < n - 1:
		raw16 = (track_bytes[i] << 8) | track_bytes[i + 1]

		if raw16 == _FM_IDAM:
			# IDAM: next 4 decoded bytes = C H R N, then 2 CRC bytes
			# Each decoded byte = 2 raw bytes
			idam_data_start = i + 2
			if idam_data_start + 10 > n:
				i += 2
				continue
			cyl   = _decode_fm_byte((track_bytes[idam_data_start]     << 8) | track_bytes[idam_data_start + 1])
			head  = _decode_fm_byte((track_bytes[idam_data_start + 2] << 8) | track_bytes[idam_data_start + 3])
			sect  = _decode_fm_byte((track_bytes[idam_data_start + 4] << 8) | track_bytes[idam_data_start + 5])
			size_code = _decode_fm_byte((track_bytes[idam_data_start + 6] << 8) | track_bytes[idam_data_start + 7])
			# 2 CRC raw bytes (each CRC byte = 2 raw bytes = 4 raw bytes total)
			declared_size = 128 << size_code
			pending_idam = {
				'cyl': cyl, 'head': head, 'sect': sect,
				'size_code': size_code, 'declared_size': declared_size,
				'byte_offset_idam': i,
				'byte_offset_dam': None,
				'dam_type': None,
				'data': None,
				'crc_valid': False,
				'size_used': declared_size,
				'size_was_overridden': False,
			}
			i += 2
			continue

		if raw16 in (_FM_DAM, _FM_DDAM):
			dam_type = 'DAM' if raw16 == _FM_DAM else 'DDAM'
			dam_raw_offset = i
			# Data starts at i+2 (raw bytes); each decoded byte = 2 raw bytes
			data_raw_start = i + 2
			# Determine sector size from pending IDAM (or default 128 bytes)
			declared_size = 128
			if pending_idam is not None:
				declared_size = pending_idam['declared_size']

			# Decode sector data: declared_size decoded bytes = declared_size*2 raw bytes
			decoded_payload = bytearray()
			for j in range(declared_size):
				pos = data_raw_start + j * 2
				if pos + 1 >= n:
					break
				decoded_payload.append(_decode_fm_byte((track_bytes[pos] << 8) | track_bytes[pos + 1]))

			# CRC: 2 decoded bytes = 4 raw bytes after data
			crc_raw_start = data_raw_start + declared_size * 2
			crc_bytes_decoded = bytearray()
			for j in range(2):
				pos = crc_raw_start + j * 2
				if pos + 1 >= n:
					break
				crc_bytes_decoded.append(_decode_fm_byte((track_bytes[pos] << 8) | track_bytes[pos + 1]))

			# Verify CRC: covers DAM byte + data
			dam_byte = 0xFB if dam_type == 'DAM' else 0xF8
			crc_valid = False
			size_used = declared_size
			size_was_overridden = False
			if len(crc_bytes_decoded) == 2:
				stored = (crc_bytes_decoded[0] << 8) | crc_bytes_decoded[1]
				computed = _crc16(bytes([dam_byte]) + bytes(decoded_payload))
				crc_valid = (computed == stored)

			if not crc_valid:
				# Try other sizes
				for sz in _VALID_SECTOR_SIZES:
					if sz == declared_size:
						continue
					alt_payload = bytearray()
					for j in range(sz):
						pos = data_raw_start + j * 2
						if pos + 1 >= n:
							break
						alt_payload.append(_decode_fm_byte((track_bytes[pos] << 8) | track_bytes[pos + 1]))
					alt_crc_raw = data_raw_start + sz * 2
					alt_crc = bytearray()
					for j in range(2):
						pos = alt_crc_raw + j * 2
						if pos + 1 >= n:
							break
						alt_crc.append(_decode_fm_byte((track_bytes[pos] << 8) | track_bytes[pos + 1]))
					if len(alt_crc) == 2:
						stored2 = (alt_crc[0] << 8) | alt_crc[1]
						comp2 = _crc16(bytes([dam_byte]) + bytes(alt_payload))
						if comp2 == stored2:
							decoded_payload = alt_payload
							crc_valid = True
							size_used = sz
							size_was_overridden = True
							break

			record = {
				'dam_type': dam_type,
				'data': bytes(decoded_payload),
				'crc_valid': crc_valid,
				'size_used': size_used,
				'size_was_overridden': size_was_overridden,
				'byte_offset_dam': dam_raw_offset,
			}
			if pending_idam is not None:
				pending_idam.update(record)
				sectors.append(pending_idam)
				pending_idam = None
			# else: orphan DAM with no preceding IDAM — skip

			i += 2
			continue

		i += 2

	# Flush any IDAM with no DAM
	if pending_idam is not None:
		sectors.append(pending_idam)

	return sectors


def _walk_mfm_stream(track_bytes: bytes) -> list[dict]:
	"""Decode MFM bitstream.

	Scans for three consecutive 0x4489 sync words followed by an address
	mark byte.  Reads IDAM (C H R N + CRC) and DAM/DDAM (data + CRC).

	Returns list of sector dicts in the same schema as _walk_fm_stream().
	"""
	sectors: list[dict] = []
	n = len(track_bytes)

	# Build list of sync positions: runs of 3+ consecutive 0x4489 words
	i = 0
	sync_positions: list[int] = []
	while i < n - 5:
		if (track_bytes[i] == 0x44 and track_bytes[i + 1] == 0x89 and
		    track_bytes[i + 2] == 0x44 and track_bytes[i + 3] == 0x89 and
		    track_bytes[i + 4] == 0x44 and track_bytes[i + 5] == 0x89):
			# Three sync words; mark position after the third
			sync_positions.append(i + 6)
			i += 6
		else:
			i += 1

	pending_idam: dict | None = None

	for sync_end in sync_positions:
		if sync_end >= n:
			continue
		mark = track_bytes[sync_end]

		if mark == 0xFE:
			# IDAM: C H R N + 2 CRC bytes
			if sync_end + 7 >= n:
				continue
			cyl       = track_bytes[sync_end + 1]
			head      = track_bytes[sync_end + 2]
			sect      = track_bytes[sync_end + 3]
			size_code = track_bytes[sync_end + 4]
			declared_size = 128 << size_code

			# CRC check for IDAM itself (3 sync A1 bytes + mark + 4 bytes)
			idam_crc_input = b'\xA1\xA1\xA1' + bytes([0xFE, cyl, head, sect, size_code])
			stored_crc = struct.unpack('>H', track_bytes[sync_end + 5:sync_end + 7])[0]
			idam_crc_valid = (_crc16(idam_crc_input) == stored_crc)
			if not idam_crc_valid:
				log.debug("MFM IDAM CRC mismatch at offset %d", sync_end)

			pending_idam = {
				'cyl': cyl, 'head': head, 'sect': sect,
				'size_code': size_code, 'declared_size': declared_size,
				'byte_offset_idam': sync_end,
				'byte_offset_dam': None,
				'dam_type': None,
				'data': None,
				'crc_valid': False,
				'size_used': declared_size,
				'size_was_overridden': False,
			}

		elif mark in (0xFB, 0xF8):
			dam_type = 'DAM' if mark == 0xFB else 'DDAM'
			data_start = sync_end + 1
			declared_size = 128
			if pending_idam is not None:
				declared_size = pending_idam['declared_size']

			sr = read_sector_search_size(track_bytes, data_start, declared_size)

			record = {
				'dam_type': dam_type,
				'data': sr['data'],
				'crc_valid': sr['crc_valid'],
				'size_used': sr['size_used'],
				'size_was_overridden': sr['size_was_overridden'],
				'byte_offset_dam': sync_end,
			}
			if pending_idam is not None:
				pending_idam.update(record)
				sectors.append(pending_idam)
				pending_idam = None

	if pending_idam is not None:
		sectors.append(pending_idam)

	return sectors


def walk_track(track_bytes: bytes, encoding: str) -> list[dict]:
	"""Walk a de-interleaved, opcode-stripped track byte stream.

	Dispatches to _walk_mfm_stream or _walk_fm_stream based on encoding.
	encoding: 'mfm' | 'amiga_mfm' | 'fm' | 'unknown'

	Returns list of sector dicts (see _walk_mfm_stream / _walk_fm_stream
	for the schema).
	"""
	if encoding in ('mfm', 'amiga_mfm', 'unknown'):
		return _walk_mfm_stream(track_bytes)
	elif encoding == 'fm':
		return _walk_fm_stream(track_bytes)
	else:
		log.warning("walk_track: unknown encoding %r, attempting MFM", encoding)
		return _walk_mfm_stream(track_bytes)


# ---------------------------------------------------------------------------
# Mastering format detection helpers
# ---------------------------------------------------------------------------

_TRACEBACK_SIG = b'TRACEBACK'
_BCD_TS_SIG    = bytes([0x01, 0x02, 0x03, 0x04, 0x05])
_BCD_TS_MIN_SIZE = 0x5B + 8   # need at least to the end of text_b


def _bcd(b: int) -> int:
	"""Decode a BCD byte to an integer."""
	return (b >> 4) * 10 + (b & 0x0F)


def _try_traceback(data: bytes, track: int, side: int) -> dict | None:
	"""Return a TRACEBACK indicator dict if the signature is present."""
	idx = data.find(_TRACEBACK_SIG)
	if idx < 0:
		return None
	# Collect null-separated fields starting after the signature
	rest = data[idx + len(_TRACEBACK_SIG):]
	fields = []
	for raw_field in rest.split(b'\x00'):
		text = raw_field.decode('latin-1', errors='replace').strip()
		if text:
			fields.append(text)
	return {
		'type':   'traceback',
		'track':  track,
		'side':   side,
		'fields': fields,
	}


def _try_bcd_timestamp(data: bytes, track: int, side: int,
                       declared_size: int, size_used: int,
                       size_was_overridden: bool, crc_valid: bool) -> dict | None:
	"""Return a bcd_timestamp_record indicator if the format matches."""
	if len(data) < _BCD_TS_MIN_SIZE:
		return None
	if data[0:5] != _BCD_TS_SIG:
		return None

	year   = _bcd(data[14])
	month  = _bcd(data[15])
	day    = _bcd(data[16])
	hour   = _bcd(data[17])
	minute = _bcd(data[18])
	second = _bcd(data[19])

	timestamp = f"{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"

	text_a_raw = data[23:23 + 64]
	text_a = text_a_raw.split(b'\x00')[0].decode('latin-1', errors='replace')

	text_b_raw = data[0x5B:0x5B + 8]
	text_b = text_b_raw.decode('latin-1', errors='replace').rstrip()

	return {
		'type':               'bcd_timestamp_record',
		'track':              track,
		'side':               side,
		'declared_size':      declared_size,
		'actual_size':        size_used,
		'size_was_overridden': size_was_overridden,
		'crc_valid':          crc_valid,
		'timestamp':          timestamp,
		'text_a':             text_a,
		'text_b':             text_b,
	}


# ---------------------------------------------------------------------------
# High-level: mastering analysis
# ---------------------------------------------------------------------------

def analyse_hfe_mastering(path: Path, scan_count: int = 5) -> dict:
	"""Decode sector content of the last scan_count tracks; detect mastering.

	Scans both known mastering formats:
	  - TRACEBACK  (MFM, null-separated text, magic b'TRACEBACK')
	  - BCD timestamp record  (FM, PC-format, signature 01 02 03 04 05)

	Returns:
	  hfe_version          str   ('v1', 'v2', 'v3', 'unknown')
	  trailing_track_count int   how many tracks were scanned
	  indicators           list  of indicator dicts
	"""
	header = parse_hfe_header(path)
	n_tracks   = header['n_tracks']
	n_sides    = header['n_sides']
	hfe_ver    = header['version']
	encoding   = header['track_encoding']
	track_list = header['track_list']

	first_scan = max(0, n_tracks - scan_count)
	scanned    = n_tracks - first_scan

	indicators: list[dict] = []

	with open(path, 'rb') as f:
		for t_idx in range(first_scan, n_tracks):
			if t_idx >= len(track_list):
				break
			entry = track_list[t_idx]

			for side in range(n_sides):
				try:
					track_bytes, _weak = get_track_bytes(f, entry, side, hfe_version=hfe_ver if isinstance(hfe_ver, int) else 1)
				except Exception as e:
					log.warning("HFE mastering: failed to read track %d side %d: %s", t_idx, side, e)
					continue

				# Walk the track with whatever encoding the header declares.
				# TRACEBACK is MFM; BCD timestamp is FM.  A mastering track
				# could contain either format (or both), so we try both walkers
				# when encoding is ambiguous.
				encodings_to_try = [encoding]
				if encoding == 'unknown':
					encodings_to_try = ['mfm', 'fm']

				for enc in encodings_to_try:
					sectors = walk_track(track_bytes, enc)
					for sector in sectors:
						data = sector.get('data')
						if not data:
							continue

						ind = _try_traceback(data, t_idx, side)
						if ind:
							indicators.append(ind)
							continue

						ind = _try_bcd_timestamp(
							data, t_idx, side,
							declared_size=sector['declared_size'],
							size_used=sector['size_used'],
							size_was_overridden=sector['size_was_overridden'],
							crc_valid=sector['crc_valid'],
						)
						if ind:
							indicators.append(ind)

	return {
		'hfe_version':          header['hfe_version_str'],
		'trailing_track_count': scanned,
		'indicators':           indicators,
	}


# ---------------------------------------------------------------------------
# High-level: protection analysis
# ---------------------------------------------------------------------------

def analyse_hfe_protection(path: Path) -> dict:
	"""Scan all HFE tracks for copy protection indicators.

	Detects the following per-track anomalies:

	  weak_bits       RAND opcodes present (v2/v3 HFE), indicating fuzzy/
	                  uncertain bits used by some protection schemes.
	  bad_crc         Sector with an invalid data-field CRC — may be
	                  intentional (protection reads and *expects* the error).
	  id_mismatch     Sector IDAM cylinder does not match the physical track
	                  number — a classic copy protection trick.
	  ddam            Sector uses a Deleted Data Address Mark (0xF8) — used
	                  by some protection schemes to trigger CRC errors in
	                  standard DOS but still carry readable data.
	  duplicate_id    Same C/H/R/N sector ID appears more than once on a
	                  track — some protections use duplicate IDs to confuse
	                  copy programs.

	Returns:
	  hfe_version   str    ('v1', 'v2', 'v3', 'unknown')
	  indicators    list   of indicator dicts (see above)
	"""
	header = parse_hfe_header(path)
	n_tracks   = header['n_tracks']
	n_sides    = header['n_sides']
	hfe_ver    = header['version']
	encoding   = header['track_encoding']
	track_list = header['track_list']

	indicators: list[dict] = []

	with open(path, 'rb') as f:
		for t_idx in range(n_tracks):
			if t_idx >= len(track_list):
				break
			entry = track_list[t_idx]

			for side in range(n_sides):
				try:
					track_bytes, weak_offsets = get_track_bytes(
						f, entry, side,
						hfe_version=hfe_ver if isinstance(hfe_ver, int) else 1,
					)
				except Exception as e:
					log.warning("HFE protection: failed to read track %d side %d: %s", t_idx, side, e)
					continue

				# Weak / fuzzy bits (RAND opcodes in v2/v3 only)
				if weak_offsets:
					indicators.append({
						'type':    'weak_bits',
						'track':   t_idx,
						'side':    side,
						'count':   len(weak_offsets),
						'offsets': weak_offsets[:16],  # cap for report size
					})

				sectors = walk_track(track_bytes, encoding)

				seen_ids: dict[tuple, int] = {}

				for sector in sectors:
					cyl  = sector.get('cyl')
					head = sector.get('head')
					sect = sector.get('sect')
					size_code = sector.get('size_code')

					# Deleted data address mark
					if sector.get('dam_type') == 'DDAM':
						indicators.append({
							'type':  'ddam',
							'track': t_idx,
							'side':  side,
							'cyl':   cyl,
							'head':  head,
							'sect':  sect,
						})

					# Bad CRC (only flag when data was actually read)
					if not sector.get('crc_valid', True) and sector.get('data') is not None:
						indicators.append({
							'type':  'bad_crc',
							'track': t_idx,
							'side':  side,
							'cyl':   cyl,
							'head':  head,
							'sect':  sect,
						})

					# Cylinder mismatch — sector ID claims a different track
					if cyl is not None and cyl != t_idx:
						indicators.append({
							'type':       'id_mismatch',
							'track':      t_idx,
							'side':       side,
							'sector_cyl': cyl,
							'sect':       sect,
						})

					# Collect IDs for duplicate detection
					sid = (cyl, head, sect, size_code)
					seen_ids[sid] = seen_ids.get(sid, 0) + 1

				# Duplicate sector IDs
				for (cyl, head, sect, size_code), count in seen_ids.items():
					if count > 1:
						indicators.append({
							'type':      'duplicate_id',
							'track':     t_idx,
							'side':      side,
							'cyl':       cyl,
							'head':      head,
							'sect':      sect,
							'size_code': size_code,
							'count':     count,
						})

	return {
		'hfe_version': header['hfe_version_str'],
		'indicators':  indicators,
	}

# vim: ts=4 sw=4 noet
