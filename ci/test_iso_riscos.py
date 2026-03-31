"""
Tests for ISO 9660 RISC OS filetype extraction (ARCHIMEDES extension).

Tests the internal helpers in worker/arcworker/tools/iso_riscos.py using
synthetic data only — no ISO image file required.
"""

import struct
import sys
import os
import unittest

# Add the repo root to sys.path so imports work without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from worker.arcworker.tools.iso_riscos import (
    _get_riscos_filetype,
    _parse_archimedes_block,
    _get_nm_name,
)


def _make_archimedes_block(load_addr: int, exec_addr: int = 0, attrs: int = 0) -> bytes:
    """Build a synthetic 32-byte ARCHIMEDES System Use block."""
    return b'ARCHIMEDES' + struct.pack('<III', load_addr, exec_addr, attrs) + b'\x00' * 10


def _make_nm_entry(name: bytes, flags: int = 0) -> bytes:
    """Build a synthetic Rock Ridge NM System Use entry."""
    entry_len = 5 + len(name)
    return b'NM' + bytes([entry_len, 1, flags]) + name


class TestGetRiscosFiletype(unittest.TestCase):
    """Tests for _get_riscos_filetype(): load address → filetype hex string."""

    def test_typed_file_fff(self):
        # Load address 0xFFFFFFF0: bits 31:20 = 0xFFF, bits 19:8 = 0xFFF → type 'fff'
        self.assertEqual(_get_riscos_filetype(0xFFFFFFF0), 'fff')

    def test_typed_file_ffb(self):
        # Filetype 0xFFB (BASIC): load = 0xFFFFFFB0? No: bits 19:8 = 0xFFB
        # (load_addr >> 8) & 0xFFF = 0xFFB  →  load_addr & 0xFFFFF00 = 0xFFB00
        # Top 12 bits 0xFFF: load_addr = 0xFFF_FFB_xx  → 0xFFFFfb00 (xx=0)
        # Actually: (load >> 20) must be 0xFFF. (load >> 8) & 0xFFF must be 0xFFB.
        # load = (0xFFF << 20) | (0xFFB << 8) | 0x00 = 0xFFFFFF B00 → 0xFFFFFB00
        self.assertEqual(_get_riscos_filetype(0xFFFFFB00), 'ffb')

    def test_typed_file_ddc(self):
        # Filetype 0xDDC (Archive): (0xFFF << 20) | (0xDDC << 8) = 0xFFFDDC00
        self.assertEqual(_get_riscos_filetype(0xFFFDDC00), 'ddc')

    def test_typed_file_ff8(self):
        # Filetype 0xFF8 (Absolute): (0xFFF << 20) | (0xFF8 << 8) = 0xFFFFf800
        self.assertEqual(_get_riscos_filetype(0xFFFFF800), 'ff8')

    def test_untyped_raw_load_addr(self):
        # Old-style load address — not date-stamped format
        self.assertIsNone(_get_riscos_filetype(0x00000000))

    def test_untyped_partial_prefix(self):
        # Bits 31:20 = 0xFF0, not 0xFFF
        self.assertIsNone(_get_riscos_filetype(0xFF000000))

    def test_untyped_high_bit_only(self):
        self.assertIsNone(_get_riscos_filetype(0x80000000))

    def test_filetype_zero(self):
        # Filetype 0x000: (0xFFF << 20) | (0x000 << 8) = 0xFFF00000
        self.assertEqual(_get_riscos_filetype(0xFFF00000), '000')


class TestParseArchimedesBlock(unittest.TestCase):
    """Tests for _parse_archimedes_block(): full ARCHIMEDES block → (filetype, pling)."""

    def test_basic_filetype_extraction(self):
        # Filetype 0xFFF (Text), no pling
        block = _make_archimedes_block(load_addr=0xFFFFFFF0, attrs=0x000)
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertEqual(filetype, 'fff')
        self.assertFalse(has_pling)

    def test_pling_flag_set(self):
        # Attributes bit 0x100 set → filename originally started with '!'
        block = _make_archimedes_block(load_addr=0xFFFFFB00, attrs=0x100)
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertEqual(filetype, 'ffb')
        self.assertTrue(has_pling)

    def test_pling_flag_not_set(self):
        block = _make_archimedes_block(load_addr=0xFFFFFB00, attrs=0x000)
        _, has_pling = _parse_archimedes_block(block)
        self.assertFalse(has_pling)

    def test_pling_flag_with_other_attr_bits(self):
        # Other attribute bits must not affect the pling detection
        block = _make_archimedes_block(load_addr=0xFFFFFFF0, attrs=0x1FF)
        _, has_pling = _parse_archimedes_block(block)
        self.assertTrue(has_pling)

    def test_no_archimedes_signature(self):
        # System Use area starts with Rock Ridge SP entry, not ARCHIMEDES
        block = b'SP' + b'\x07\x01\xbe\xef\x00' + b'\x00' * 27
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertIsNone(filetype)
        self.assertFalse(has_pling)

    def test_truncated_block_under_minimum(self):
        # Only 15 bytes — not enough to hold load+exec+attrs
        block = b'ARCHIMEDES' + b'\x00' * 5
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertIsNone(filetype)
        self.assertFalse(has_pling)

    def test_empty_system_use(self):
        filetype, has_pling = _parse_archimedes_block(b'')
        self.assertIsNone(filetype)
        self.assertFalse(has_pling)

    def test_untyped_load_addr(self):
        # Load address does not encode a filetype
        block = _make_archimedes_block(load_addr=0x00008023, attrs=0x000)
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertIsNone(filetype)
        self.assertFalse(has_pling)

    def test_archimedes_block_followed_by_more_data(self):
        # Extra bytes after the block are ignored
        block = _make_archimedes_block(load_addr=0xFFFDDC00) + b'NM\x0a\x01\x00extra'
        filetype, has_pling = _parse_archimedes_block(block)
        self.assertEqual(filetype, 'ddc')
        self.assertFalse(has_pling)


class TestGetNmName(unittest.TestCase):
    """Tests for _get_nm_name(): Rock Ridge NM entry parsing."""

    def test_simple_name(self):
        su = _make_nm_entry(b'readme.txt')
        self.assertEqual(_get_nm_name(su), 'readme.txt')

    def test_name_with_filetype_suffix(self):
        # Suffix is preserved — parse_acorn_filename() strips it separately
        su = _make_nm_entry(b'!Run,feb')
        self.assertEqual(_get_nm_name(su), '!Run,feb')

    def test_name_pling_no_suffix(self):
        su = _make_nm_entry(b'!Paint')
        self.assertEqual(_get_nm_name(su), '!Paint')

    def test_no_nm_entry(self):
        # Only an SP SUSP entry — no NM
        sp = b'SP\x07\x01\xbe\xef\x00'
        self.assertIsNone(_get_nm_name(sp))

    def test_empty_system_use(self):
        self.assertIsNone(_get_nm_name(b''))

    def test_nm_after_archimedes_block(self):
        # ARCHIMEDES block (32 bytes) followed by NM entry
        archimedes = _make_archimedes_block(load_addr=0xFFFFFFF0)
        nm = _make_nm_entry(b'myfile.txt')
        su = archimedes + nm
        self.assertEqual(_get_nm_name(su), 'myfile.txt')

    def test_nm_without_archimedes_block(self):
        # No ARCHIMEDES block — NM is at offset 0
        nm = _make_nm_entry(b'plain.txt')
        self.assertEqual(_get_nm_name(nm), 'plain.txt')

    def test_continuation_flag_returns_first_part_only(self):
        # NM entry with flags=1 (more NM entries follow); we return what we have
        nm = _make_nm_entry(b'partial', flags=0x01)
        result = _get_nm_name(nm)
        # Should return the first part without crashing
        self.assertEqual(result, 'partial')

    def test_nm_after_other_susp_entry(self):
        # SP entry followed by NM (no ARCHIMEDES block)
        sp = b'SP\x07\x01\xbe\xef\x00'
        nm = _make_nm_entry(b'after_sp.txt')
        su = sp + nm
        self.assertEqual(_get_nm_name(su), 'after_sp.txt')

    def test_zero_length_entry_stops_scan(self):
        # A zero-length entry is a terminator; NM after it is unreachable
        nm = _make_nm_entry(b'unreachable.txt')
        su = b'\x00\x00' + nm
        self.assertIsNone(_get_nm_name(su))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
