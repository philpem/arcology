"""
Unit tests for ICS / Baildon Electronics IDEFS partition detection.

Tests the checksum validation, entry parsing (including end markers and
deleted entries), protection flag decoding, password hash extraction,
and full disc-image detection via synthetic images.

No external tools or database required — these exercise only the pure-Python
partition detection code in worker/arcworker/tools/partition.py.

Run:
    python -m unittest ci.test_ics_partition -v
"""

import os
import struct
import sys
import tempfile
import unittest

# Ensure the repo root is on sys.path so ``worker`` is importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools.partition import (
    ICS_CHECKSUM_OFFSET,
    ICS_CHECKSUM_SEED,
    ICS_ENTRY_SIZE,
    ICS_PARTITION_TABLE_SIZE,
    ICS_PROTECTION_OFFSET,
    ICS_PASSWORD_HASH_OFFSET,
    ICS_SECTOR_SIZE,
    ICS_TOTAL_CAPACITY_OFFSET,
    FILECORE_BOOT_BLOCK_OFFSET,
    FILECORE_BOOT_BLOCK_SIZE,
    FILECORE_BB_DISC_RECORD_OFFSET,
    _validate_ics_checksum,
    _decode_ics_protection,
    _extract_ics_password_hashes,
    _detect_ics_partitions,
)


def _build_ics_sector0(entries, total_capacity_sectors=0):
    """Build a valid ICS IDEFS sector 0 with the given partition entries.

    *entries* is a list of (start_sector, size_sectors) tuples.
    Returns a 512-byte ``bytes`` with a correct "Part" checksum.
    """
    buf = bytearray(ICS_PARTITION_TABLE_SIZE)

    for i, (start, size) in enumerate(entries):
        off = i * ICS_ENTRY_SIZE
        struct.pack_into('<II', buf, off, start, size & 0xFFFFFFFF)

    struct.pack_into('<I', buf, ICS_TOTAL_CAPACITY_OFFSET, total_capacity_sectors)

    # Compute checksum
    checksum = ICS_CHECKSUM_SEED
    for i in range(ICS_CHECKSUM_OFFSET):
        checksum += buf[i]
    checksum &= 0xFFFFFFFF
    struct.pack_into('<I', buf, ICS_CHECKSUM_OFFSET, checksum)

    return bytes(buf)


def _build_boot_block(disc_size=0x1E8BE000, disc_name=b'TestDisc',
                      protection=0, hash_lo=0, hash_hi=0xFFFFFFFF,
                      log2_sector_size=9, sectors_per_track=63, heads=16):
    """Build a 512-byte FileCore boot block with valid checksum."""
    bb = bytearray(FILECORE_BOOT_BLOCK_SIZE)

    # Protection flags
    bb[ICS_PROTECTION_OFFSET] = protection & 0xFF

    # Password hashes
    struct.pack_into('<I', bb, ICS_PASSWORD_HASH_OFFSET, hash_lo)
    struct.pack_into('<I', bb, ICS_PASSWORD_HASH_OFFSET + 4, hash_hi)

    # FileCore disc record at 0x1C0
    dr_off = FILECORE_BB_DISC_RECORD_OFFSET
    bb[dr_off + 0] = log2_sector_size
    bb[dr_off + 1] = sectors_per_track
    bb[dr_off + 2] = heads
    bb[dr_off + 4] = 0x0F  # ID length
    bb[dr_off + 5] = 0x0A  # log2(bytes per map bit)
    struct.pack_into('<I', bb, dr_off + 0x10, disc_size)

    # Disc name at dr_off + 0x16
    name = disc_name[:10].ljust(10, b'\r')
    bb[dr_off + 0x16:dr_off + 0x16 + 10] = name

    # Boot block checksum: carry-propagation sum of bytes 0x1FE..0x000
    # (walking backwards), result stored at byte 0x1FF.
    s = 0
    for i in range(510, -1, -1):
        s += bb[i]
        s = (s & 0xFF) + (s >> 8)
    bb[511] = s & 0xFF

    return bytes(bb)


def _build_disc_image(entries, total_capacity_sectors=0, boot_blocks=None):
    """Build a minimal synthetic ICS IDEFS disc image.

    Returns a bytes object large enough to contain sector 0 and the boot
    blocks for all partitions listed in *entries*.
    """
    sector0 = _build_ics_sector0(entries, total_capacity_sectors)

    # Determine required image size
    max_end = ICS_PARTITION_TABLE_SIZE
    for start, size in entries:
        if size == 0:
            break
        if size & 0x80000000:
            continue
        part_end = start * ICS_SECTOR_SIZE + size * ICS_SECTOR_SIZE
        bb_end = start * ICS_SECTOR_SIZE + FILECORE_BOOT_BLOCK_OFFSET + FILECORE_BOOT_BLOCK_SIZE
        max_end = max(max_end, part_end, bb_end)

    image = bytearray(max_end)
    image[:ICS_PARTITION_TABLE_SIZE] = sector0

    # Place boot blocks
    if boot_blocks:
        for (start_sector, _size), bb in zip(entries, boot_blocks):
            if bb is None:
                continue
            bb_offset = start_sector * ICS_SECTOR_SIZE + FILECORE_BOOT_BLOCK_OFFSET
            image[bb_offset:bb_offset + len(bb)] = bb

    return bytes(image)


class TestICSChecksum(unittest.TestCase):
    """Tests for _validate_ics_checksum."""

    def test_valid_checksum(self):
        sector0 = _build_ics_sector0([(0, 0x0F9800)])
        self.assertTrue(_validate_ics_checksum(sector0))

    def test_invalid_checksum_corrupted_byte(self):
        sector0 = bytearray(_build_ics_sector0([(0, 0x0F9800)]))
        sector0[4] ^= 0xFF  # corrupt one byte without updating checksum
        self.assertFalse(_validate_ics_checksum(bytes(sector0)))

    def test_all_zeros_fails(self):
        """All-zero sector 0 should fail (seed is non-zero)."""
        self.assertFalse(_validate_ics_checksum(bytes(512)))

    def test_too_short_fails(self):
        self.assertFalse(_validate_ics_checksum(bytes(256)))

    def test_example_from_spec(self):
        """Verify the worked example from partition_ics_idefs.md."""
        buf = bytearray(512)
        # 4-partition layout from the spec
        entries = [
            (0x00000000, 0x000F9800),
            (0x000F9800, 0x000F9800),
            (0x001F3000, 0x000F9800),
            (0x002EC800, 0x000E47E0),
        ]
        for i, (start, size) in enumerate(entries):
            struct.pack_into('<II', buf, i * 8, start, size)
        struct.pack_into('<I', buf, 0x1F8, 0x003D0FE0)
        # Expected checksum: 0x506178B6
        struct.pack_into('<I', buf, 0x1FC, 0x506178B6)
        self.assertTrue(_validate_ics_checksum(bytes(buf)))


class TestICSProtection(unittest.TestCase):
    """Tests for _decode_ics_protection."""

    def test_level_0_none(self):
        result = _decode_ics_protection(0x00)
        self.assertEqual(result['level'], 0)
        self.assertEqual(result['summary'], 'none')

    def test_level_1_rw_password(self):
        result = _decode_ics_protection(0x01)
        self.assertEqual(result['level'], 1)
        self.assertIn('read/write', result['summary'])

    def test_level_2_ro_password(self):
        result = _decode_ics_protection(0x02)
        self.assertEqual(result['level'], 2)
        self.assertIn('read only', result['summary'])

    def test_level_3_no_access(self):
        result = _decode_ics_protection(0x03)
        self.assertEqual(result['level'], 3)
        self.assertIn('no access', result['summary'])

    def test_upper_bits_ignored(self):
        """Upper bits should not affect the protection level."""
        result = _decode_ics_protection(0xFC)
        self.assertEqual(result['level'], 0)


class TestICSPasswordHashes(unittest.TestCase):
    """Tests for _extract_ics_password_hashes."""

    def test_zero_hashes(self):
        bb = bytearray(512)
        result = _extract_ics_password_hashes(bytes(bb))
        self.assertEqual(result['hash_lo'], '0x00000000')
        self.assertEqual(result['hash_hi'], '0x00000000')

    def test_known_hashes(self):
        bb = bytearray(512)
        struct.pack_into('<I', bb, ICS_PASSWORD_HASH_OFFSET, 0xDEADBEEF)
        struct.pack_into('<I', bb, ICS_PASSWORD_HASH_OFFSET + 4, 0xCAFEBABE)
        result = _extract_ics_password_hashes(bytes(bb))
        self.assertEqual(result['hash_lo'], '0xDEADBEEF')
        self.assertEqual(result['hash_hi'], '0xCAFEBABE')


class TestDetectICSPartitions(unittest.TestCase):
    """Integration tests for _detect_ics_partitions with synthetic images."""

    def _write_and_detect(self, image_data):
        """Write image to a temp file and run detection."""
        with tempfile.NamedTemporaryFile(suffix='.dd', delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name
        try:
            from pathlib import Path
            return _detect_ics_partitions(Path(tmp_path))
        finally:
            os.unlink(tmp_path)

    def test_single_partition(self):
        entries = [(0, 0x0F9800)]
        bb = _build_boot_block(disc_name=b'MyDisc')
        image = _build_disc_image(entries, 0x0F9800, [bb])
        result = self._write_and_detect(image)

        self.assertTrue(result['detected'])
        self.assertEqual(result['scheme'], 'ics_idefs')
        self.assertEqual(len(result['partitions']), 1)

        p = result['partitions'][0]
        self.assertEqual(p['index'], 0)
        self.assertEqual(p['start_byte'], 0)
        self.assertEqual(p['size_bytes'], 0x0F9800 * 512)
        self.assertEqual(p['filesystem'], 'adfs')
        self.assertTrue(p['boot_block_valid'])
        self.assertEqual(p['disc_name'], 'MyDisc')
        self.assertIsNotNone(p['protection'])
        self.assertIsNotNone(p['password_hash'])

    def test_four_partitions(self):
        """Spec example: 4 partitions on a ~1.9GB drive."""
        entries = [
            (0x00000000, 0x000F9800),
            (0x000F9800, 0x000F9800),
            (0x001F3000, 0x000F9800),
            (0x002EC800, 0x000E47E0),
        ]
        bbs = [
            _build_boot_block(disc_name=b'Part0'),
            _build_boot_block(disc_name=b'Part1'),
            _build_boot_block(disc_name=b'Part2'),
            _build_boot_block(disc_name=b'Part3'),
        ]
        image = _build_disc_image(entries, 0x003D0FE0, bbs)
        result = self._write_and_detect(image)

        self.assertTrue(result['detected'])
        self.assertEqual(len(result['partitions']), 4)
        self.assertEqual(result['total_capacity_sectors'], 0x003D0FE0)
        for i, p in enumerate(result['partitions']):
            self.assertEqual(p['index'], i)
            self.assertEqual(p['disc_name'], f'Part{i}')

    def test_end_marker(self):
        """A zero-size entry should stop parsing."""
        entries = [
            (0, 0x0F9800),
            (0, 0),  # end marker
            (0x1F3000, 0x0F9800),  # should be ignored
        ]
        bb = _build_boot_block()
        image = _build_disc_image([(0, 0x0F9800)], 0x0F9800, [bb])
        # Rebuild sector0 with the end marker
        sector0 = _build_ics_sector0(entries, 0x0F9800)
        image = bytearray(image)
        image[:512] = sector0
        result = self._write_and_detect(bytes(image))

        self.assertTrue(result['detected'])
        self.assertEqual(len(result['partitions']), 1)

    def test_deleted_entry_skipped(self):
        """A negative size (bit 31 set) entry should be skipped."""
        entries = [
            (0, 0x0F9800),
            (0x0F9800, 0x80000000 | 0x0F9800),  # deleted
            (0x1F3000, 0x0F9800),
        ]
        bbs = [
            _build_boot_block(disc_name=b'First'),
            None,
            _build_boot_block(disc_name=b'Third'),
        ]
        image = _build_disc_image(entries, 0x2EC800, bbs)
        result = self._write_and_detect(image)

        self.assertTrue(result['detected'])
        self.assertEqual(len(result['partitions']), 2)
        self.assertEqual(result['partitions'][0]['disc_name'], 'First')
        self.assertEqual(result['partitions'][1]['disc_name'], 'Third')
        # Indices should be sequential valid-partition indices
        self.assertEqual(result['partitions'][0]['index'], 0)
        self.assertEqual(result['partitions'][1]['index'], 1)

    def test_max_four_partitions(self):
        """Should stop at 4 valid partitions even if more entries exist."""
        entries = [(i * 0x10000, 0x10000) for i in range(6)]
        bbs = [_build_boot_block(disc_name=f'P{i}'.encode()) for i in range(6)]
        image = _build_disc_image(entries, 0x60000, bbs)
        result = self._write_and_detect(image)

        self.assertTrue(result['detected'])
        self.assertEqual(len(result['partitions']), 4)

    def test_invalid_checksum_rejected(self):
        """Image with corrupted checksum should not be detected."""
        sector0 = bytearray(_build_ics_sector0([(0, 0x0F9800)]))
        sector0[ICS_CHECKSUM_OFFSET] ^= 0xFF  # corrupt checksum
        image = bytearray(0x0F9800 * 512)
        image[:512] = sector0
        result = self._write_and_detect(bytes(image))
        self.assertFalse(result['detected'])

    def test_too_small_image(self):
        result = self._write_and_detect(bytes(256))
        self.assertFalse(result['detected'])

    def test_protection_and_password_extracted(self):
        """Verify protection flags and password hashes are present."""
        entries = [(0, 0x0F9800)]
        bb = _build_boot_block(protection=0x02, hash_lo=0x12345678, hash_hi=0xABCDEF00)
        image = _build_disc_image(entries, 0x0F9800, [bb])
        result = self._write_and_detect(image)

        p = result['partitions'][0]
        self.assertEqual(p['protection']['level'], 2)
        self.assertIn('read only', p['protection']['summary'])
        self.assertEqual(p['password_hash']['hash_lo'], '0x12345678')
        self.assertEqual(p['password_hash']['hash_hi'], '0xABCDEF00')


if __name__ == '__main__':
    unittest.main()
# vim: ts=4 sw=4 et
