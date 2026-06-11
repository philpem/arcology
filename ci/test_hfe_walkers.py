"""
Characterization tests for the HFE FM/MFM bit walkers.

Builds synthetic FM and MFM bitstreams with known sector layouts and
asserts the exact records produced by _walk_fm_stream / _walk_mfm_stream:
CHRN fields, CRC validation, data payloads, deleted-data marks, the
declared-size-override search, IDAM/DAM pairing, and the byte-offset
field semantics (which intentionally differ between the two encodings).

Written before unifying the two walkers so the refactor is provably
behaviour-preserving.
"""

import binascii
import os
import sys
import unittest
import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _bits_of_word(value: int) -> list[int]:
    """16-bit pattern as a bit list, MSB first."""
    return [(value >> (15 - i)) & 1 for i in range(16)]


# ---------------------------------------------------------------------------
# MFM builder
# ---------------------------------------------------------------------------

_MFM_SYNC_BITS = _bits_of_word(0x4489)   # A1 with missing clock


class MfmTrackBuilder:
    """Accumulates an MFM bitstream and records emit positions."""

    def __init__(self):
        self.bits: list[int] = []
        self.prev = 0   # previous data bit, for the clock rule

    def byte(self, b: int):
        for i in range(7, -1, -1):
            d = (b >> i) & 1
            clock = 1 if (self.prev == 0 and d == 0) else 0
            self.bits.extend((clock, d))
            self.prev = d

    def data(self, payload: bytes):
        for b in payload:
            self.byte(b)

    def gap(self, count: int, fill: int = 0x4E):
        self.data(bytes([fill]) * count)

    def sync_triple(self) -> int:
        """Emit 12x00 + 3x A1-sync.  Returns the bit position of the first sync."""
        self.data(b'\x00' * 12)
        pos = len(self.bits)
        for _ in range(3):
            self.bits.extend(_MFM_SYNC_BITS)
        self.prev = 1   # A1 ends with data bit 1
        return pos

    def idam(self, cyl: int, head: int, sect: int, size_code: int) -> int:
        """Emit a full IDAM field.  Returns the bit position of the FE mark."""
        self.sync_triple()
        mark_bit = len(self.bits)
        chrn = bytes([cyl, head, sect, size_code])
        crc = binascii.crc_hqx(b'\xA1\xA1\xA1\xFE' + chrn, 0xFFFF)
        self.byte(0xFE)
        self.data(chrn + bytes([crc >> 8, crc & 0xFF]))
        return mark_bit

    def dam(self, payload: bytes, deleted: bool = False,
            crc_size: int | None = None) -> int:
        """Emit a data field.  Returns the bit position of the mark byte.

        crc_size: compute the CRC over only the first crc_size bytes of
        payload (placed immediately after them), to build sectors whose
        true size differs from the IDAM's declared size.
        """
        self.sync_triple()
        mark_bit = len(self.bits)
        mark = 0xF8 if deleted else 0xFB
        size = crc_size if crc_size is not None else len(payload)
        crc = binascii.crc_hqx(b'\xA1\xA1\xA1' + bytes([mark]) + payload[:size], 0xFFFF)
        self.byte(mark)
        self.data(payload[:size] + bytes([crc >> 8, crc & 0xFF]) + payload[size:])
        return mark_bit

    def track_bytes(self) -> bytes:
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return np.packbits(np.array(bits, dtype=np.uint8)).tobytes()


# ---------------------------------------------------------------------------
# FM builder
# ---------------------------------------------------------------------------

_FM_MARKS = {'idam': 0xF57E, 'dam': 0xF56F, 'ddam': 0xF56A}


class FmTrackBuilder:
    """Accumulates an FM bitstream (clock bits all 1 except in marks)."""

    def __init__(self):
        self.bits: list[int] = []

    def byte(self, b: int):
        for i in range(7, -1, -1):
            self.bits.extend((1, (b >> i) & 1))

    def data(self, payload: bytes):
        for b in payload:
            self.byte(b)

    def gap(self, count: int, fill: int = 0xFF):
        self.data(bytes([fill]) * count)

    def mark(self, kind: str) -> int:
        """Emit an address-mark word.  Returns the bit position AFTER it
        (the walkers' event anchor for FM)."""
        self.data(b'\x00' * 6)
        self.bits.extend(_bits_of_word(_FM_MARKS[kind]))
        return len(self.bits)

    def idam(self, cyl: int, head: int, sect: int, size_code: int) -> int:
        after_mark = self.mark('idam')
        chrn = bytes([cyl, head, sect, size_code])
        crc = binascii.crc_hqx(bytes([0xFE]) + chrn, 0xFFFF)
        self.data(chrn + bytes([crc >> 8, crc & 0xFF]))
        return after_mark

    def dam(self, payload: bytes, deleted: bool = False) -> int:
        after_mark = self.mark('ddam' if deleted else 'dam')
        mark = 0xF8 if deleted else 0xFB
        crc = binascii.crc_hqx(bytes([mark]) + payload, 0xFFFF)
        self.data(payload + bytes([crc >> 8, crc & 0xFF]))
        return after_mark

    def track_bytes(self) -> bytes:
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return np.packbits(np.array(bits, dtype=np.uint8)).tobytes()


# ---------------------------------------------------------------------------
# MFM walker tests
# ---------------------------------------------------------------------------

class TestMfmWalker(unittest.TestCase):

    def _walk(self, builder):
        from worker.arcworker.tools.hfe import _walk_mfm_stream
        return _walk_mfm_stream(builder.track_bytes())

    def test_two_good_sectors(self):
        b = MfmTrackBuilder()
        b.gap(32)
        payload1 = bytes(range(256))
        idam1 = b.idam(2, 0, 1, 1)
        b.gap(22)
        dam1 = b.dam(payload1)
        b.gap(32)
        payload2 = bytes((i * 7) & 0xFF for i in range(512))
        idam2 = b.idam(2, 0, 2, 2)
        b.gap(22)
        dam2 = b.dam(payload2)
        b.gap(64)

        sectors = self._walk(b)
        self.assertEqual(len(sectors), 2)

        s1, s2 = sectors
        self.assertEqual((s1['cyl'], s1['head'], s1['sect'], s1['size_code']), (2, 0, 1, 1))
        self.assertEqual(s1['declared_size'], 256)
        self.assertEqual(s1['dam_type'], 'DAM')
        self.assertTrue(s1['crc_valid'])
        self.assertEqual(s1['data'], payload1)
        self.assertEqual(s1['size_used'], 256)
        self.assertFalse(s1['size_was_overridden'])
        self.assertEqual(s1['_enc'], 'mfm')
        # MFM byte offsets are mark-word bit position >> 3 (raw stream bytes)
        self.assertEqual(s1['byte_offset_idam'], idam1 >> 3)
        self.assertEqual(s1['byte_offset_dam'], dam1 >> 3)
        # _data_start_bit is the first bit after the decoded mark byte
        self.assertEqual(s1['_data_start_bit'], dam1 + 16)

        self.assertEqual((s2['sect'], s2['declared_size']), (2, 512))
        self.assertTrue(s2['crc_valid'])
        self.assertEqual(s2['data'], payload2)
        self.assertEqual(s2['byte_offset_idam'], idam2 >> 3)
        self.assertEqual(s2['byte_offset_dam'], dam2 >> 3)

    def test_deleted_data_mark(self):
        b = MfmTrackBuilder()
        b.gap(32)
        b.idam(0, 1, 5, 0)
        b.gap(22)
        b.dam(bytes(128), deleted=True)
        b.gap(32)
        (s,) = self._walk(b)
        self.assertEqual(s['dam_type'], 'DDAM')
        self.assertTrue(s['crc_valid'])
        self.assertEqual(s['head'], 1)

    def test_size_override_search(self):
        # IDAM declares 512 bytes but the sector really holds 256 + CRC.
        b = MfmTrackBuilder()
        b.gap(32)
        payload = bytes((i * 3) & 0xFF for i in range(512))
        b.idam(0, 0, 1, 2)        # declared 512
        b.gap(22)
        b.dam(payload, crc_size=256)
        b.gap(64)
        (s,) = self._walk(b)
        self.assertTrue(s['crc_valid'])
        self.assertEqual(s['size_used'], 256)
        self.assertTrue(s['size_was_overridden'])
        self.assertEqual(s['declared_size'], 512)
        self.assertEqual(s['data'], payload[:256])

    def test_idam_without_dam_is_kept(self):
        b = MfmTrackBuilder()
        b.gap(32)
        b.idam(1, 0, 3, 1)
        b.gap(64)
        (s,) = self._walk(b)
        self.assertEqual(s['sect'], 3)
        self.assertIsNone(s['data'])
        self.assertIsNone(s['dam_type'])
        self.assertFalse(s['crc_valid'])

    def test_orphan_dam_is_skipped(self):
        b = MfmTrackBuilder()
        b.gap(32)
        b.idam(0, 0, 1, 0)
        b.gap(22)
        b.dam(bytes(128))
        b.gap(22)
        b.dam(bytes(128))          # second DAM has no pending IDAM
        b.gap(32)
        sectors = self._walk(b)
        self.assertEqual(len(sectors), 1)

    def test_empty_track(self):
        b = MfmTrackBuilder()
        b.gap(64)
        self.assertEqual(self._walk(b), [])


# ---------------------------------------------------------------------------
# FM walker tests
# ---------------------------------------------------------------------------

class TestFmWalker(unittest.TestCase):

    def _walk(self, builder):
        from worker.arcworker.tools.hfe import _walk_fm_stream
        return _walk_fm_stream(builder.track_bytes())

    def test_single_good_sector(self):
        b = FmTrackBuilder()
        b.gap(16)
        payload = bytes((i * 5) & 0xFF for i in range(128))
        idam = b.idam(1, 0, 4, 0)
        b.gap(11)
        dam = b.dam(payload)
        b.gap(32)

        (s,) = self._walk(b)
        self.assertEqual((s['cyl'], s['head'], s['sect'], s['size_code']), (1, 0, 4, 0))
        self.assertEqual(s['declared_size'], 128)
        self.assertEqual(s['dam_type'], 'DAM')
        self.assertTrue(s['crc_valid'])
        self.assertEqual(s['data'], payload)
        self.assertEqual(s['_enc'], 'fm')
        self.assertEqual(s['_bits_step'], 1)
        # FM byte offsets are the after-mark bit position >> 4 (decoded bytes)
        self.assertEqual(s['byte_offset_idam'], idam >> 4)
        self.assertEqual(s['byte_offset_dam'], dam >> 4)
        self.assertEqual(s['_data_start_bit'], dam)

    def test_deleted_data_mark(self):
        b = FmTrackBuilder()
        b.gap(16)
        b.idam(0, 0, 2, 0)
        b.gap(11)
        b.dam(bytes(range(128)), deleted=True)
        b.gap(32)
        (s,) = self._walk(b)
        self.assertEqual(s['dam_type'], 'DDAM')
        self.assertTrue(s['crc_valid'])

    def test_idam_without_dam_is_kept(self):
        b = FmTrackBuilder()
        b.gap(16)
        b.idam(0, 0, 9, 1)
        b.gap(32)
        (s,) = self._walk(b)
        self.assertEqual(s['sect'], 9)
        self.assertIsNone(s['data'])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
