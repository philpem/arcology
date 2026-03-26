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
v3 (HXCHFEV3):  any byte with LOW nibble 0xF is an opcode (file stores
                bits LSB-first within each byte; data bytes are bit-reversed
                before use):
  0x0F = NOP
  0x8F = SETINDEX
  0x4F = SETBITRATE  — 1 payload byte
  0xCF = SKIPBITS    — 1 payload byte (bits to skip in following byte)
  0x2F = RAND        — 1 payload byte (weak bits)
"""

import binascii
import logging
import struct
from pathlib import Path
from typing import BinaryIO

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_SECTOR_SIZES = (128, 256, 512, 1024, 2048, 4096, 8192)

# ---------------------------------------------------------------------------
# Tunable analysis thresholds
# ---------------------------------------------------------------------------

# Protection scan — weak-bit noise filtering
# Tracks with fewer than this many RAND-opcode positions are treated as
# noise or a write-splice artifact and no weak_bits indicator is emitted.
WEAK_BITS_MIN_COUNT = 8

# Protection scan — whole-track weak suppression
# Ratio of weak-bit positions to total decoded events (clean bytes +
# weak positions).  If this fraction meets or exceeds the threshold the
# track is assumed to be fully erased / unformatted and no weak_bits
# indicator is emitted.  The denominator is (n_weak + n_clean) rather
# than n_clean alone, because RAND opcodes are stripped from the clean
# output so n_weak can exceed len(track_bytes) on heavily-weak tracks.
WEAK_BITS_WHOLE_TRACK_RATIO = 0.75

# Mastering scan — backwards optimisation
# When True the scan iterates from the last track backwards, stopping as
# soon as a track whose sector-data fill ratio exceeds MASTERING_SCAN_STOP_FILL
# is seen (a normal data track reached).  Set to False to always scan a
# fixed forward window of scan_count tracks.
MASTERING_SCAN_OPTIMISE = True

# Mastering scan — data-track detection threshold
# Fill ratio (sum of declared sector data bytes / raw track byte length) at
# or above which a track is considered a normal data track, triggering the
# early-exit when MASTERING_SCAN_OPTIMISE is True.
MASTERING_SCAN_STOP_FILL = 0.50

# FM address mark raw 16-bit patterns (clock|data interleaved)
_FM_IDAM  = 0xF57E   # clock 0xC7, data 0xFE
_FM_DAM   = 0xF56F   # clock 0xC7, data 0xFB
_FM_DDAM  = 0xF56A   # clock 0xC7, data 0xF8

# MFM sync word
_MFM_SYNC = 0x4489   # A1 with missing clock

# Pre-computed power-of-2 arrays shared by the vectorised bit walkers.
# _POWERS16: weight vector for assembling a uint16 from 16 individual bits.
# _POWERS8:  weight vector for assembling a uint8 from 8 data bits.
_POWERS16 = np.array([1 << (15 - i) for i in range(16)], dtype=np.uint32)
_POWERS8  = np.array([128, 64, 32, 16, 8, 4, 2, 1],      dtype=np.uint16)

# Byte-reversal lookup: _BYTE_REVERSE[b] is byte b with its bits mirrored.
# Used to convert HFEv3 data bytes from LSB-first to MSB-first order.
_BYTE_REVERSE = bytes(int(f'{b:08b}'[::-1], 2) for b in range(256))


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

    # De-interleave: pick the relevant 256-byte half of each 512-byte block.
    # Pad to a multiple of 512 then reshape to (blocks, 512) and slice the side.
    raw_arr = np.frombuffer(raw, dtype=np.uint8)
    pad = (-len(raw_arr)) % 512
    if pad:
        raw_arr = np.concatenate([raw_arr, np.zeros(pad, dtype=np.uint8)])
    side_bytes = raw_arr.reshape(-1, 512)[:, side * 256:(side + 1) * 256].ravel()

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
        # Convert from file's LSB-first bit order to MSB-first for the walker
        # (same normalisation applied to v3 below).
        clean = bytearray(_BYTE_REVERSE[b] for b in clean)
    elif hfe_version == 3:
        # In HFEv3 data bytes are stored LSB-first within each byte.
        # Any byte whose LOW nibble is 0xF is an opcode; all others are data.
        # Opcode values (as stored in the file, before bit-reversal):
        #   0x0F NOP, 0x8F SETINDEX, 0x4F SETBITRATE (+1 payload),
        #   0xCF SKIPBITS (+1 payload), 0x2F RAND (+1 payload)
        while i < n:
            b = src[i]
            if (b & 0x0F) == 0x0F:
                opcode = b
                if opcode == 0x0F:    # NOP
                    i += 1
                elif opcode == 0x8F:  # SETINDEX
                    i += 1
                elif opcode == 0x4F:  # SETBITRATE
                    i += 2
                elif opcode == 0xCF:  # SKIPBITS
                    i += 2
                elif opcode == 0x2F:  # RAND (weak bits)
                    weak_offsets.append(len(clean))
                    i += 2
                else:
                    i += 1
            else:
                clean.append(b)
                i += 1
        # Convert from file's LSB-first bit order to MSB-first for the walker.
        clean = bytearray(_BYTE_REVERSE[b] for b in clean)
    else:
        # v1 or unknown: no opcodes, but data bytes are stored LSB-first within
        # each byte; reverse to MSB-first for the walker.
        clean = bytearray(_BYTE_REVERSE[b] for b in src)

    log.debug("get_track_bytes: side %d → %d bytes, %d weak-bit position(s)",
              side, len(clean), len(weak_offsets))
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
        computed = binascii.crc_hqx(crc_input, 0xFFFF)
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

def _build_bits(data: bytes, lsb_first: bool = False, step: int = 1) -> np.ndarray:
    """Unpack bytes into a flat array of bits.

    lsb_first=False (default): bit 7 (MSB) of each byte is first in time.
    lsb_first=True:            bit 0 (LSB) of each byte is first in time.
    step > 1:                  decimate the bit stream (take every step-th bit).
                               step=2 recovers FM data captured at MFM sample
                               rate, where each FM bit cell occupies 2 HFE bits.
    """
    arr = np.frombuffer(data, dtype=np.uint8)
    bits = np.unpackbits(arr, bitorder='little' if lsb_first else 'big')
    return bits[::step] if step > 1 else bits


def _walk_fm_bits(bits: list[int]) -> list[dict]:
    """Decode FM bitstream at bit level.

    Slides a 16-bit window over the bit stream and identifies FM address
    marks by their combined clock+data patterns (_FM_IDAM / _FM_DAM /
    _FM_DDAM).  After each address mark the subsequent bits are decoded
    16 at a time using _decode_fm_byte.

    FM CRC covers the address-mark byte + data (no preceding sync bytes).
    """
    n = len(bits)

    # Vectorised 16-bit sliding window scan for FM address marks.
    # FM clock bits are at even positions (0,2,...,14); data bits at odd (1,3,...,15)
    # within each 16-bit word, giving the same layout as MFM for bit extraction.
    events: list[tuple[int, int]] = []
    if n >= 16:
        windows = np.lib.stride_tricks.sliding_window_view(bits, 16)
        window_vals = windows @ _POWERS16
        for am_val, am_byte in ((_FM_IDAM, 0xFE), (_FM_DAM, 0xFB), (_FM_DDAM, 0xF8)):
            for pos in np.where(window_vals == am_val)[0]:
                events.append((int(pos) + 16, am_byte))
        events.sort()

    log.debug("_walk_fm_bits: %d bits → %d FM address mark(s)", n, len(events))

    def _fm_read(start: int, count: int) -> tuple[bytes | None, int]:
        """Decode count FM bytes from bit position start.

        FM data bits are at odd positions within each 16-bit clock+data word
        (positions 1,3,5,...,15), identical layout to MFM.
        """
        end = start + count * 16
        if end > n:
            return None, start
        words = bits[start:end].reshape(count, 16)[:, 1::2]
        return bytes((words @ _POWERS8).astype(np.uint8)), end

    sectors: list[dict] = []
    pending_idam: dict | None = None

    for bit_after, am_byte in events:
        if am_byte == 0xFE:
            # IDAM: C H R N CRC_H CRC_L  (6 decoded bytes follow the address mark)
            tail, _ = _fm_read(bit_after, 6)
            if tail is None:
                continue
            cyl, head, sect, size_code = tail[0], tail[1], tail[2], tail[3]
            declared_size = 128 << size_code
            # FM IDAM CRC covers: FE C H R N (no A1 sync prefix for FM)
            stored_crc  = (tail[4] << 8) | tail[5]
            idam_crc_ok = (binascii.crc_hqx(bytes([0xFE]) + tail[:4], 0xFFFF) == stored_crc)
            log.debug("  FM IDAM @bit%d: C=%d H=%d R=%d N=%d idam_crc=%s",
                      bit_after, cyl, head, sect, size_code,
                      'ok' if idam_crc_ok else 'FAIL')
            pending_idam = {
                'cyl': cyl, 'head': head, 'sect': sect,
                'size_code': size_code, 'declared_size': declared_size,
                'byte_offset_idam': bit_after >> 4,
                'byte_offset_dam': None,
                'dam_type': None,
                'data': None,
                'crc_valid': False,
                'size_used': declared_size,
                'size_was_overridden': False,
            }

        else:
            # DAM or DDAM
            dam_type = 'DAM' if am_byte == 0xFB else 'DDAM'
            declared_size = pending_idam['declared_size'] if pending_idam else 128

            # Try declared_size first; if CRC fails, search other valid sizes.
            # FM CRC: CRC(am_byte + data)  — no A1 prefix for FM.
            p_and_crc, _ = _fm_read(bit_after, declared_size + 2)
            if p_and_crc is not None:
                stored  = (p_and_crc[declared_size] << 8) | p_and_crc[declared_size + 1]
                payload = p_and_crc[:declared_size]
                crc_valid = (binascii.crc_hqx(bytes([am_byte]) + payload, 0xFFFF) == stored)
            else:
                payload, crc_valid = b'', False
            size_used         = declared_size
            size_was_overridden = False

            if not crc_valid:
                log.debug("  FM size-search: declared %dB CRC failed; trying alternatives", declared_size)
                for sz in _VALID_SECTOR_SIZES:
                    if sz == declared_size:
                        continue
                    alt_pc, _ = _fm_read(bit_after, sz + 2)
                    if alt_pc is None:
                        log.debug("  FM size-search: %dB — not enough bits in track", sz)
                        continue
                    stored2 = (alt_pc[sz] << 8) | alt_pc[sz + 1]
                    alt_ok = (binascii.crc_hqx(bytes([am_byte]) + alt_pc[:sz], 0xFFFF) == stored2)
                    log.debug("  FM size-search: %dB → CRC %s", sz, 'ok' if alt_ok else 'FAIL')
                    if alt_ok:
                        payload, crc_valid = alt_pc[:sz], True
                        size_used, size_was_overridden = sz, True
                        break

            log.debug("  FM DAM  @bit%d: type=%s size=%d crc=%s%s",
                      bit_after, dam_type, size_used,
                      'ok' if crc_valid else 'FAIL',
                      ' [overridden]' if size_was_overridden else '')
            record = {
                'dam_type': dam_type,
                'data': payload,
                'crc_valid': crc_valid,
                'size_used': size_used,
                'size_was_overridden': size_was_overridden,
                'byte_offset_dam': bit_after >> 4,
                '_data_start_bit': bit_after,
                '_enc': 'fm',
            }
            if pending_idam is not None:
                pending_idam.update(record)
                sectors.append(pending_idam)
                pending_idam = None
            # else: orphan DAM — skip

    if pending_idam is not None:
        sectors.append(pending_idam)

    return sectors


def _walk_fm_stream(track_bytes: bytes) -> list[dict]:
    """Decode FM bitstream.

    Converts the raw bytes to a bitstream and scans at bit level for FM
    address-mark patterns (see _walk_fm_bits).

    get_track_bytes() normalises all HFE variants to MSB-first before calling
    here, so only MSB-first is tried.  LSB-first is not a valid state after
    that normalisation.

    Two step values are still attempted because some HFE files store FM
    tracks sampled at MFM bit-rate (step=2: each FM bit cell occupies 2 HFE
    sample bits) while others use native FM density (step=1).  The step that
    yields more sectors is kept.

    Returns list of sector dicts:
      cyl, head, sect, size_code, declared_size
      dam_type   'DAM' | 'DDAM' | None
      data       bytes (sector payload) or None if only IDAM seen
      crc_valid  bool
      size_used  int
      size_was_overridden  bool
      byte_offset_idam  int   (approximate bit_pos >> 4)
      byte_offset_dam   int or None
    """
    best: list[dict] = []
    best_step = 1
    for step in (1, 2):
        sectors = _walk_fm_bits(_build_bits(track_bytes, step=step))
        if len(sectors) > len(best):
            best = sectors
            best_step = step
    for s in best:
        s['_bits_lsb_first'] = False
        s['_bits_step'] = best_step
    return best


def _walk_mfm_bits(bits: list[int]) -> list[dict]:
    """Decode MFM bitstream at bit level.

    Slides a 16-bit window over the entire bitstream looking for the A1
    sync pattern (0x4489 — MFM encoding of 0xA1 with a missing clock bit).
    MFM word boundaries are not necessarily aligned to byte boundaries in
    the HFE file, so the search operates one bit at a time.

    Three consecutive 0x4489 syncs delimit each sector header (IDAM) and
    data field (DAM/DDAM).  After locating a triple sync, subsequent 16-bit
    MFM words are decoded (data bits are at odd positions 1,3,5,...,15) to
    recover the address-mark byte, CHRN, CRC, and sector data.

    MFM CRC covers A1 A1 A1 + address-mark-byte + data/CHRN bytes.
    """
    n = len(bits)

    # Vectorised 16-bit sliding window scan for the 0x4489 sync pattern.
    if n >= 16:
        windows = np.lib.stride_tricks.sliding_window_view(bits, 16)
        window_vals = windows @ _POWERS16
        sync_starts: set[int] = set(np.where(window_vals == _MFM_SYNC)[0].tolist())
    else:
        sync_starts = set()

    # Triple sync: three consecutive syncs at offsets 0, 16, 32 bits.
    # The address mark (or IAM) byte starts at offset 48 from the first sync.
    triple_data_bits: list[int] = sorted(
        s + 48 for s in sync_starts
        if (s + 16) in sync_starts and (s + 32) in sync_starts
    )

    log.debug("_walk_mfm_bits: %d bits → %d raw syncs, %d triple sync(s)",
              n, len(sync_starts), len(triple_data_bits))

    def _mfm_byte(start: int) -> tuple[int | None, int]:
        """Decode one MFM byte: data bits at positions 1,3,5,...,15 of a 16-bit window."""
        if start + 16 > n:
            return None, start
        val = int(bits[start + 1:start + 16:2] @ _POWERS8)
        return val, start + 16

    def _mfm_read(start: int, count: int) -> tuple[bytes | None, int]:
        end = start + count * 16
        if end > n:
            return None, start
        words = bits[start:end].reshape(count, 16)[:, 1::2]
        return bytes((words @ _POWERS8).astype(np.uint8)), end

    sectors: list[dict] = []
    pending_idam: dict | None = None

    for data_bit in triple_data_bits:
        mark, pos = _mfm_byte(data_bit)
        if mark is None:
            continue

        if mark == 0xFE:
            # IDAM: C H R N CRC_H CRC_L  (6 decoded bytes after the mark)
            tail, _ = _mfm_read(pos, 6)
            if tail is None:
                continue
            cyl, head, sect, size_code = tail[0], tail[1], tail[2], tail[3]
            declared_size = 128 << size_code
            # MFM IDAM CRC covers: A1 A1 A1 FE C H R N
            stored_crc    = (tail[4] << 8) | tail[5]
            idam_crc_ok   = (binascii.crc_hqx(b'\xA1\xA1\xA1\xFE' + tail[:4], 0xFFFF) == stored_crc)
            log.debug("  MFM IDAM @bit%d: C=%d H=%d R=%d N=%d idam_crc=%s",
                      data_bit, cyl, head, sect, size_code,
                      'ok' if idam_crc_ok else 'FAIL')
            pending_idam = {
                'cyl': cyl, 'head': head, 'sect': sect,
                'size_code': size_code, 'declared_size': declared_size,
                'byte_offset_idam': data_bit >> 3,
                'byte_offset_dam': None,
                'dam_type': None,
                'data': None,
                'crc_valid': False,
                'size_used': declared_size,
                'size_was_overridden': False,
            }

        elif mark in (0xFB, 0xF8):
            # DAM or DDAM
            dam_type      = 'DAM' if mark == 0xFB else 'DDAM'
            declared_size = pending_idam['declared_size'] if pending_idam else 128

            # Try declared_size first; search other sizes if CRC fails.
            # MFM DAM CRC covers: A1 A1 A1 mark data
            p_and_crc, _ = _mfm_read(pos, declared_size + 2)
            if p_and_crc is not None:
                stored    = (p_and_crc[declared_size] << 8) | p_and_crc[declared_size + 1]
                payload   = p_and_crc[:declared_size]
                crc_valid = (binascii.crc_hqx(b'\xA1\xA1\xA1' + bytes([mark]) + payload, 0xFFFF) == stored)
            else:
                payload, crc_valid = b'', False
            size_used          = declared_size
            size_was_overridden = False

            if not crc_valid:
                log.debug("  MFM size-search: declared %dB CRC failed; trying alternatives", declared_size)
                for sz in _VALID_SECTOR_SIZES:
                    if sz == declared_size:
                        continue
                    alt_pc, _ = _mfm_read(pos, sz + 2)
                    if alt_pc is None:
                        log.debug("  MFM size-search: %dB — not enough bits in track", sz)
                        continue
                    stored2 = (alt_pc[sz] << 8) | alt_pc[sz + 1]
                    alt_ok = (binascii.crc_hqx(b'\xA1\xA1\xA1' + bytes([mark]) + alt_pc[:sz], 0xFFFF) == stored2)
                    log.debug("  MFM size-search: %dB → CRC %s", sz, 'ok' if alt_ok else 'FAIL')
                    if alt_ok:
                        payload, crc_valid = alt_pc[:sz], True
                        size_used, size_was_overridden = sz, True
                        break

            log.debug("  MFM DAM  @bit%d: type=%s size=%d crc=%s%s",
                      data_bit, dam_type, size_used,
                      'ok' if crc_valid else 'FAIL',
                      ' [overridden]' if size_was_overridden else '')
            record = {
                'dam_type': dam_type,
                'data': payload,
                'crc_valid': crc_valid,
                'size_used': size_used,
                'size_was_overridden': size_was_overridden,
                'byte_offset_dam': data_bit >> 3,
                '_data_start_bit': pos,
                '_enc': 'mfm',
            }
            if pending_idam is not None:
                pending_idam.update(record)
                sectors.append(pending_idam)
                pending_idam = None
        # 0xFC (IAM) and other marks are ignored — no data follows

    if pending_idam is not None:
        sectors.append(pending_idam)

    return sectors


def _walk_mfm_stream(track_bytes: bytes) -> list[dict]:
    """Decode MFM bitstream.

    Converts the raw bytes to a bitstream and scans at bit level for A1
    sync patterns (0x4489) at any bit-phase alignment (see _walk_mfm_bits).

    get_track_bytes() normalises all HFE variants to MSB-first before calling
    here, so only MSB-first is tried.  LSB-first is not a valid state after
    that normalisation.

    Returns list of sector dicts (same schema as _walk_fm_stream).
    """
    sectors = _walk_mfm_bits(_build_bits(track_bytes))
    for s in sectors:
        s['_bits_lsb_first'] = False
        s['_bits_step'] = 1
    return sectors


def walk_track(track_bytes: bytes, encoding: str) -> list[dict]:
    """Walk a de-interleaved, opcode-stripped track byte stream.

    Dispatches to _walk_mfm_stream or _walk_fm_stream based on encoding.
    encoding: 'mfm' | 'amiga_mfm' | 'fm' | 'unknown'

    Returns list of sector dicts (see _walk_mfm_stream / _walk_fm_stream
    for the schema).
    """
    log.debug("walk_track: %d bytes, encoding=%r", len(track_bytes), encoding)
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
    # Walk back to the start of the null-terminated field that contains the
    # signature (e.g. b'$TRACEBACK\x99') so we include the complete string
    # rather than just what follows the keyword.
    field_start = data.rfind(b'\x00', 0, idx)
    field_start = field_start + 1 if field_start >= 0 else 0
    # Collect null-separated fields from there onward.
    # Discard entries that contain no alphanumeric characters.
    fields = []
    for raw_field in data[field_start:].split(b'\x00'):
        text = raw_field.decode('latin-1', errors='replace').strip()
        if text and any(c.isalnum() for c in text):
            fields.append(text)
    return {
        'type':   'traceback',
        'track':  track,
        'side':   side,
        'fields': fields,
    }


def _try_formaster(data: bytes, track: int, side: int,
                   declared_size: int, size_used: int,
                   size_was_overridden: bool, crc_valid: bool) -> dict | None:
    """Return a formaster indicator if the format matches.

    Field layout (offsets in the sector data):
      0x00-0x04  signature (5 bytes: \x01\x02\x03\x04\x05)
      0x0E-0x13  BCD timestamp (year, month, day, hour, minute, second)
      0x17-0x26  format_code (16 bytes, space-padded)
      0x27-0x56  format_description (48 bytes, space-padded, null-terminated)
      0x5B-0x62  serial_number (8 bytes)
      0x82-0xB1  text_c (48 bytes, space-padded, null-terminated; 256-byte record only)
    """
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

    # text_a is two sub-fields: 16-byte space-padded format code followed
    # by 48-byte space-padded, null-terminated format description.
    format_code_raw = data[0x17:0x27]
    format_code = format_code_raw.rstrip(b' \x00').decode('latin-1', errors='replace')

    format_desc_raw = data[0x27:0x57]
    format_description = format_desc_raw.split(b'\x00')[0].decode('latin-1', errors='replace').rstrip()

    # text_b is a machine serial number (8 bytes)
    serial_number_raw = data[0x5B:0x5B + 8]
    serial_number = serial_number_raw.decode('latin-1', errors='replace').rstrip()

    result = {
        'type':               'formaster',
        'track':              track,
        'side':               side,
        'declared_size':      declared_size,
        'actual_size':        size_used,
        'size_was_overridden': size_was_overridden,
        'crc_valid':          crc_valid,
        'timestamp':          timestamp,
        'format_code':        format_code,
        'format_description': format_description,
        'serial_number':      serial_number,
    }

    # Extended fields present in 256-byte record
    if len(data) >= 0x81 + 48:
        text_c_raw = data[0x81:0x81 + 48]
        result['text_c'] = text_c_raw.split(b'\x00')[0].decode('latin-1', errors='replace').rstrip()

    return result


# ---------------------------------------------------------------------------
# String extraction helper
# ---------------------------------------------------------------------------

def _extract_strings(data: bytes, min_len: int = 4) -> list[str]:
    """Extract runs of printable characters from raw sector data.

    Finds sequences of printable Latin-1 characters (ASCII printable
    0x20-0x7E plus Latin-1 extended 0xA0-0xFF) of at least min_len
    chars.  Equivalent to the strings(1) utility.
    """
    results: list[str] = []
    current: list[str] = []
    for b in data:
        if 0x20 <= b <= 0x7E or 0xA0 <= b <= 0xFF:
            current.append(chr(b))
        else:
            if len(current) >= min_len:
                results.append(''.join(current))
            current = []
    if len(current) >= min_len:
        results.append(''.join(current))
    return results

# ---------------------------------------------------------------------------
# BCD timestamp forced-read helper
# ---------------------------------------------------------------------------

def _force_read_sector_256(track_bytes: bytes, sector: dict) -> tuple[bytes | None, bool]:
    """Re-decode exactly 256 bytes from a sector's data-start bit position.

    Used by the mastering scanner to force a 256-byte read for BCD timestamp
    records when the IDAM declares a smaller sector size or the CRC fails at
    256 bytes.  The sector dict must contain the '_data_start_bit',
    '_enc', '_bits_lsb_first', and '_bits_step' fields added by the walkers.

    Returns (data_256, crc_valid).  Returns (None, False) when the sector
    meta-data is missing or there are not enough track bits to decode 256 bytes.
    """
    data_start_bit = sector.get('_data_start_bit')
    if data_start_bit is None:
        return None, False

    lsb_first = sector.get('_bits_lsb_first', False)
    step      = sector.get('_bits_step', 1)
    enc       = sector.get('_enc', 'mfm')

    bits = _build_bits(track_bytes, lsb_first=lsb_first, step=step)

    # Decode 256 data bytes (each decoded byte = 16 bits; data at odd positions)
    n   = 256
    end = data_start_bit + n * 16
    if end > len(bits):
        log.debug("_force_read_sector_256: not enough bits (need %d, have %d from pos %d)",
                  end, len(bits), data_start_bit)
        return None, False
    words    = bits[data_start_bit:end].reshape(n, 16)[:, 1::2]
    data_256 = bytes((words @ _POWERS8).astype(np.uint8))

    # Recover the mark byte (one decoded byte = 16 bits before data start)
    mark_start = data_start_bit - 16
    if mark_start >= 0:
        mk = bits[mark_start:data_start_bit].reshape(1, 16)[:, 1::2]
        mark_byte = int((mk @ _POWERS8).astype(np.uint8)[0])
    else:
        mark_byte = 0xFB  # fallback: assume normal DAM

    # Decode and verify the 2 CRC bytes that follow the data
    if end + 32 > len(bits):
        return data_256, False
    crc_words  = bits[end:end + 32].reshape(2, 16)[:, 1::2]
    crc_bytes  = (crc_words @ _POWERS8).astype(np.uint8)
    stored_crc = (int(crc_bytes[0]) << 8) | int(crc_bytes[1])

    if enc == 'fm':
        crc_input = bytes([mark_byte]) + data_256
    else:
        crc_input = b'\xA1\xA1\xA1' + bytes([mark_byte]) + data_256

    crc_valid = (binascii.crc_hqx(crc_input, 0xFFFF) == stored_crc)
    return data_256, crc_valid

# ---------------------------------------------------------------------------
# High-level: mastering analysis
# ---------------------------------------------------------------------------

def analyse_hfe_mastering(path: Path, scan_count: int = 5,
                          include_raw_data: bool = False) -> dict:
    """Decode sector content of the last scan_count tracks; detect mastering.

    Scans both known mastering formats:
      - TRACEBACK  (MFM, null-separated text, magic b'TRACEBACK')
      - Formaster record  (FM, PC-format, signature 01 02 03 04 05)

    Single-sector tracks with unrecognised content are captured as
    'unknown_mastering' indicators (with extracted printable strings).

    When MASTERING_SCAN_OPTIMISE is True (default) the scan iterates
    backwards from the last track, stopping as soon as a track whose
    sector-data fill ratio exceeds MASTERING_SCAN_STOP_FILL is seen.
    This finds mastering metadata quickly without reading every data
    track.  Set MASTERING_SCAN_OPTIMISE = False to always scan a fixed
    forward window of scan_count tracks.

    When include_raw_data is True each indicator dict will contain a
    'raw_data' key whose value is the raw sector bytes (bytes object).
    This is intentionally off by default because the bytes are not
    JSON-serialisable and would break the worker API pipeline.

    Returns:
      hfe_version          str   ('v1', 'v2', 'v3', 'unknown')
      trailing_track_count int   how many tracks were actually scanned
      indicators           list  of indicator dicts
    """
    header = parse_hfe_header(path)
    n_tracks   = header['n_tracks']
    n_sides    = header['n_sides']
    hfe_ver    = header['version']
    encoding   = header['track_encoding']
    track_list = header['track_list']

    first_scan = max(0, n_tracks - scan_count)

    indicators: list[dict] = []

    # Per (track, side): list of (data, crc_valid) for unrecognised sectors.
    # Used after the scan to detect single-sector tracks with unknown format.
    side_unrecognised: dict[tuple[int, int], list[tuple[bytes, bool]]] = {}
    # Sides where at least one sector matched a known mastering format.
    side_recognised: set[tuple[int, int]] = set()

    # With MASTERING_SCAN_OPTIMISE enabled we iterate backwards from the
    # last track so we encounter mastering tracks before data tracks and can
    # stop as soon as we hit a track that looks fully populated with sectors.
    track_range = range(first_scan, n_tracks)
    track_iter  = reversed(track_range) if MASTERING_SCAN_OPTIMISE else iter(track_range)
    tracks_scanned = 0

    with open(path, 'rb') as f:
        for t_idx in track_iter:
            if t_idx >= len(track_list):
                continue
            entry = track_list[t_idx]
            tracks_scanned += 1

            # Track the maximum sector-data fill seen across all (side, encoding)
            # combinations for this track.  Used to detect normal data tracks
            # during the backwards-optimised scan.
            track_max_fill = 0.0

            for side in range(n_sides):
                try:
                    track_bytes, _weak = get_track_bytes(f, entry, side, hfe_version=hfe_ver if isinstance(hfe_ver, int) else 1)
                except Exception as e:
                    log.warning("HFE mastering: failed to read track %d side %d: %s", t_idx, side, e)
                    continue

                log.debug("mastering scan: track %d side %d (%d bytes)",
                          t_idx, side, len(track_bytes))

                # Mastering tracks routinely use a different encoding to the
                # data tracks (e.g. FM mastering on an otherwise MFM disk).
                # Always try both walkers regardless of the header encoding.

                for enc in ('mfm', 'fm'):
                    sectors = walk_track(track_bytes, enc)

                    # Measure sector-data fill for this (side, encoding) pass.
                    if MASTERING_SCAN_OPTIMISE and track_bytes:
                        sector_data_bytes = sum(
                            s.get('declared_size', 0) for s in sectors
                            if s.get('data') is not None)
                        fill = sector_data_bytes / len(track_bytes)
                        if fill > track_max_fill:
                            track_max_fill = fill

                    for sector in sectors:
                        data = sector.get('data')
                        if not data:
                            continue

                        sk = (t_idx, side)  # side key for tracking

                        ind = _try_traceback(data, t_idx, side)
                        if ind:
                            if include_raw_data:
                                ind['raw_data'] = data
                            indicators.append(ind)
                            side_recognised.add(sk)
                            continue

                        # For BCD timestamp records the sector may be declared as
                        # smaller than 256 bytes (e.g. N=0 → 128B) yet the actual
                        # record is always 256 bytes.  Force a 256-byte re-read
                        # even when the CRC at that size fails.
                        bcd_data          = data
                        bcd_crc_valid     = sector.get('crc_valid', False)
                        bcd_size_used     = sector.get('size_used', sector.get('declared_size', 0))
                        bcd_was_overridden = sector.get('size_was_overridden', False)

                        if (len(data) >= 5 and data[0:5] == _BCD_TS_SIG
                                and len(data) < 256 and '_data_start_bit' in sector):
                            forced, forced_crc = _force_read_sector_256(track_bytes, sector)
                            if forced is not None:
                                log.debug(
                                    "mastering: BCD sector track %d side %d: "
                                    "forced 256-byte read (was %d bytes, crc=%s)",
                                    t_idx, side, len(data), 'ok' if forced_crc else 'FAIL')
                                bcd_data           = forced
                                bcd_crc_valid      = forced_crc
                                bcd_size_used      = 256
                                bcd_was_overridden = (sector.get('size_used',
                                                          sector.get('declared_size', 0)) != 256)

                        ind = _try_formaster(
                            bcd_data, t_idx, side,
                            declared_size=sector['declared_size'],
                            size_used=bcd_size_used,
                            size_was_overridden=bcd_was_overridden,
                            crc_valid=bcd_crc_valid,
                        )
                        if ind:
                            if include_raw_data:
                                ind['raw_data'] = bcd_data
                            indicators.append(ind)
                            side_recognised.add(sk)
                        else:
                            # Sector not matched by any known format.
                            # Record the original (pre-force-read) data; dedup by
                            # content so MFM and FM decoding the same sector only
                            # counts once.
                            side_unrecognised.setdefault(sk, []).append(
                                (data, sector.get('crc_valid', False)))

            # Stop the backwards scan when a normally-populated data track is
            # reached; mastering tracks should all be beyond this point.
            if MASTERING_SCAN_OPTIMISE and track_max_fill >= MASTERING_SCAN_STOP_FILL:
                log.debug(
                    "mastering: track %d sector-data fill %.0f%% >= %.0f%% threshold — "
                    "looks like a data track, stopping backwards scan",
                    t_idx, track_max_fill * 100, MASTERING_SCAN_STOP_FILL * 100)
                break

    # Emit unknown_mastering for sides that yielded exactly one unique
    # sector with data and had no recognised mastering format.  A track
    # with multiple sectors is likely a normal data track near the end
    # of the disc; we ignore those to avoid false positives.
    for sk, sector_list in side_unrecognised.items():
        if sk in side_recognised:
            continue
        # Deduplicate by data content; last crc_valid for each content wins.
        unique: dict[bytes, bool] = {}
        for d, crc in sector_list:
            unique[d] = crc
        if len(unique) != 1:
            continue  # multiple distinct sectors — not a single mastering record
        data, crc_valid = next(iter(unique.items()))
        t_idx, side = sk
        # Skip sectors filled entirely with a single repeated byte.  These
        # are format-fill / blank sectors (e.g. 0xF6 IBM fill, 0xE5, 0x00)
        # that appear at the end of a disc when all sectors on the track are
        # identical but carry no meaningful content.  A real mastering record
        # always contains varied data.
        if data and len(set(data)) <= 1:
            log.debug(
                "mastering: track %d side %d: skipping uniform-fill sector "
                "(0x%02X × %d bytes) — not mastering data",
                t_idx, side, data[0], len(data))
            continue
        strings = _extract_strings(data)
        # Store up to 512 bytes of hex for examination; record actual size.
        max_hex_bytes = 512
        _ind = {
            'type':      'unknown_mastering',
            'track':     t_idx,
            'side':      side,
            'size':      len(data),
            'crc_valid': crc_valid,
            'strings':   strings,
            'data_hex':  data[:max_hex_bytes].hex(),
            'truncated': len(data) > max_hex_bytes,
        }
        if include_raw_data:
            _ind['raw_data'] = data
        indicators.append(_ind)
        log.debug("mastering: track %d side %d: unknown single-sector format (%d bytes, %d string(s))", t_idx, side, len(data), len(strings))

    # Deduplicate: if the same mastering data appears on both sides (or on
    # multiple tracks), keep only one copy.  When two copies differ only in
    # crc_valid, prefer the valid-CRC copy.
    deduped: list[dict] = []
    seen: dict[tuple, int] = {}   # content_key → index in deduped

    for ind in indicators:
        t = ind['type']
        if t == 'traceback':
            key = ('traceback', tuple(ind['fields']))
        elif t == 'formaster':
            key = ('formaster', ind['timestamp'],
                   ind.get('format_code', ''), ind.get('serial_number', ''))
        elif t == 'unknown_mastering':
            key = ('unknown_mastering', ind.get('data_hex', ''))
        else:
            key = None

        if key is None:
            deduped.append(ind)
        elif key not in seen:
            seen[key] = len(deduped)
            deduped.append(ind)
        else:
            # Already seen — upgrade to this copy if it has a valid CRC and
            # the stored copy does not.
            existing = deduped[seen[key]]
            if ind.get('crc_valid') and not existing.get('crc_valid'):
                deduped[seen[key]] = ind

    return {
        'hfe_version':          header['hfe_version_str'],
        'trailing_track_count': tracks_scanned,
        'indicators':           deduped,
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

                log.debug("protection scan: track %d side %d (%d bytes, %d weak)",
                          t_idx, side, len(track_bytes), len(weak_offsets))

                # Weak / fuzzy bits (RAND opcodes in v2/v3 only)
                # Filter out noise (too few positions) and fully-erased tracks
                # (almost all positions weak) before emitting an indicator.
                if weak_offsets:
                    n_weak    = len(weak_offsets)
                    track_len = len(track_bytes)
                    # Denominator is "total decoded events" (clean bytes + weak
                    # positions).  Using only track_len would give ratios > 100%
                    # because RAND opcodes are stripped from clean output, so
                    # n_weak can exceed len(track_bytes) on heavily-weak tracks.
                    events     = n_weak + track_len
                    weak_ratio = n_weak / events if events else 1.0
                    if n_weak < WEAK_BITS_MIN_COUNT:
                        log.debug(
                            "protection: track %d side %d: %d weak-bit position(s) "
                            "< threshold %d — treating as noise, skipping",
                            t_idx, side, n_weak, WEAK_BITS_MIN_COUNT)
                    elif weak_ratio >= WEAK_BITS_WHOLE_TRACK_RATIO:
                        log.debug(
                            "protection: track %d side %d: %.0f%% of decoded events "
                            "are weak (%d weak + %d clean) >= threshold %.0f%% — "
                            "treating as unformatted track, skipping",
                            t_idx, side, weak_ratio * 100,
                            n_weak, track_len, WEAK_BITS_WHOLE_TRACK_RATIO * 100)
                    else:
                        indicators.append({
                            'type':    'weak_bits',
                            'track':   t_idx,
                            'side':    side,
                            'count':   n_weak,
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

# vim: ts=4 sw=4 et
