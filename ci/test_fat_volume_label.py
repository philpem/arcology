"""
Unit tests for FAT filesystem volume-label extraction.

Covers :func:`worker.arcworker.tools.partition.read_fat_volume_label` and
its helper :func:`_decode_fat_label`.  Synthesises minimal-but-valid FAT12
and FAT32 boot sectors + root directories so the test runs with no
external dependencies.

Run:
    python -m unittest ci.test_fat_volume_label -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.tools.partition import (
    _decode_fat_label,
    read_fat_volume_label,
)

# =============================================================================
# _decode_fat_label
# =============================================================================

class TestDecodeFatLabel(unittest.TestCase):
    def test_trailing_spaces_stripped(self):
        self.assertEqual(_decode_fat_label(b'MYDISK     '), 'MYDISK')

    def test_empty_returns_none(self):
        self.assertIsNone(_decode_fat_label(b'           '))

    def test_no_name_sentinel_returns_none(self):
        self.assertIsNone(_decode_fat_label(b'NO NAME    '))

    def test_cp850_decoding(self):
        # 0x82 in CP850 is 'é'
        self.assertEqual(_decode_fat_label(b'CAF\x82       '), 'CAFé')

    def test_first_byte_0x05_restored_to_0xe5(self):
        # DIR_Name[0] == 0x05 means the real first byte is 0xE5 (Kanji
        # first-byte collision with the deleted-entry marker).
        # CP850 decodes 0xE5 as 'Õ'.
        self.assertEqual(_decode_fat_label(b'\x05ABC       '), 'ÕABC')

    def test_short_label(self):
        self.assertEqual(_decode_fat_label(b'A          '), 'A')

    def test_nul_padded_label(self):
        # Some formatters NUL-pad instead of space-pad; trailing NULs must be stripped.
        self.assertEqual(_decode_fat_label(b'MYDISK\x00\x00\x00\x00\x00'), 'MYDISK')

    def test_embedded_nul_truncates(self):
        # NUL is not a valid FAT label character; C-string truncation at first NUL.
        self.assertEqual(_decode_fat_label(b'LAB\x00EL      '), 'LAB')

    def test_leading_nul_returns_none(self):
        # NUL in position 0: C-string truncation leaves empty → None.
        self.assertIsNone(_decode_fat_label(b'\x00LABEL     '))


# =============================================================================
# read_fat_volume_label (synthetic images)
# =============================================================================

BYTES_PER_SECTOR = 512


def _build_fat16_image(
    bpb_label: bytes = b'BPBLABEL   ',
    root_entries: list[bytes] | None = None,
    num_fats: int = 2,
    rsvd: int = 1,
    root_ent_cnt: int = 16,
    fat_sz16: int = 1,
    fs_type: bytes = b'FAT16   ',
) -> bytes:
    """Synthesise a small FAT16 image in memory.

    The layout is deliberately tiny (1 reserved sector, 2 FAT sectors,
    one root-directory sector holding 16 entries) but every BPB field
    passes :func:`detect_fat_filesystem`'s validation.
    """
    if root_entries is None:
        root_entries = []
    if len(bpb_label) != 11:
        raise ValueError("bpb_label must be 11 bytes")
    if len(fs_type) != 8:
        raise ValueError("fs_type must be 8 bytes")

    # Boot sector (offset / field / value):
    sector = bytearray(BYTES_PER_SECTOR)
    sector[0:3]   = b'\xEB\x3C\x90'                               # jmp
    sector[3:11]  = b'MSWIN4.1'                                   # OEM
    sector[11:13] = BYTES_PER_SECTOR.to_bytes(2, 'little')        # BPB_BytsPerSec
    sector[13]    = 1                                             # BPB_SecPerClus
    sector[14:16] = rsvd.to_bytes(2, 'little')                    # BPB_RsvdSecCnt
    sector[16]    = num_fats                                      # BPB_NumFATs
    sector[17:19] = root_ent_cnt.to_bytes(2, 'little')            # BPB_RootEntCnt
    # Total sectors chosen so cluster count lands cleanly in FAT16 range.
    tot_sec16 = 8000
    sector[19:21] = tot_sec16.to_bytes(2, 'little')               # BPB_TotSec16
    sector[21]    = 0xF8                                          # BPB_Media
    sector[22:24] = fat_sz16.to_bytes(2, 'little')                # BPB_FATSz16
    sector[43:54] = bpb_label                                     # BS_VolLab
    sector[54:62] = fs_type                                       # BS_FilSysType
    sector[510:512] = b'\x55\xAA'

    # Root directory sector (one sector, 16 x 32-byte entries)
    root_sector = bytearray(BYTES_PER_SECTOR)
    offset = 0
    for entry in root_entries:
        if len(entry) != 32:
            raise ValueError("each root entry must be 32 bytes")
        root_sector[offset:offset + 32] = entry
        offset += 32

    # Layout: [boot][reserved-1][FAT1 x fat_sz16][FAT2 x fat_sz16][root][data...]
    image = bytearray()
    image += bytes(sector)
    image += b'\x00' * BYTES_PER_SECTOR * (rsvd - 1)
    image += b'\x00' * BYTES_PER_SECTOR * (num_fats * fat_sz16)
    image += bytes(root_sector)
    # Pad to declared total sector count so downstream tools don't choke.
    consumed = len(image) // BYTES_PER_SECTOR
    image += b'\x00' * BYTES_PER_SECTOR * (tot_sec16 - consumed)
    return bytes(image)


def _make_dir_entry(name11: bytes, attr: int) -> bytes:
    if len(name11) != 11:
        raise ValueError("name11 must be 11 bytes")
    entry = bytearray(32)
    entry[0:11] = name11
    entry[11]   = attr
    return bytes(entry)


class TestReadFatVolumeLabelFAT16(unittest.TestCase):
    def _write(self, data: bytes) -> Path:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.img')
        tmp.write(data)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_root_dir_label_preferred_over_bpb(self):
        # Authoritative root-dir entry should win over BS_VolLab.
        root_entry = _make_dir_entry(b'ROOTLABEL  ', 0x08)
        img = _build_fat16_image(
            bpb_label=b'BPBLABEL   ',
            root_entries=[root_entry],
        )
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'ROOTLABEL')

    def test_falls_back_to_bpb_label(self):
        # No root-dir volume entry, so BS_VolLab is used.
        img = _build_fat16_image(bpb_label=b'BPBLABEL   ')
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'BPBLABEL')

    def test_no_name_returns_none(self):
        img = _build_fat16_image(bpb_label=b'NO NAME    ')
        path = self._write(img)
        self.assertIsNone(read_fat_volume_label(path))

    def test_lfn_entries_skipped(self):
        # 0x0F attribute marks an LFN fragment; must not be treated as a
        # volume-label entry even though ATTR_VOLUME_ID is set in 0x0F.
        lfn = _make_dir_entry(b'\x41LFNENTRY  ', 0x0F)
        vol = _make_dir_entry(b'REALNAME   ', 0x08)
        img = _build_fat16_image(root_entries=[lfn, vol])
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'REALNAME')

    def test_deleted_entries_skipped(self):
        # 0xE5 in byte 0 marks a deleted entry.
        deleted = _make_dir_entry(b'\xE5OLDLABEL  ', 0x08)
        vol     = _make_dir_entry(b'NEWNAME    ', 0x08)
        img = _build_fat16_image(root_entries=[deleted, vol])
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'NEWNAME')

    def test_end_of_directory_marker_stops_scan(self):
        # 0x00 in byte 0 is the end-of-directory marker: entries after it
        # must not be scanned (they are guaranteed unused).
        terminator = b'\x00' * 32
        vol_after  = _make_dir_entry(b'SHOULDSKIP ', 0x08)
        img = _build_fat16_image(
            bpb_label=b'FALLBACK   ',
            root_entries=[terminator, vol_after],
        )
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'FALLBACK')

    def test_directory_with_volume_id_bit_ignored(self):
        # DIRECTORY bit set: spec says this is NOT a volume label entry.
        weird = _make_dir_entry(b'WEIRDDIR   ', 0x08 | 0x10)
        img = _build_fat16_image(
            bpb_label=b'FALLBACK   ',
            root_entries=[weird],
        )
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'FALLBACK')

    def test_invalid_boot_signature_returns_none(self):
        img = bytearray(_build_fat16_image())
        img[510:512] = b'\x00\x00'   # strip 0x55 0xAA
        path = self._write(bytes(img))
        self.assertIsNone(read_fat_volume_label(path))

    def test_missing_file_returns_none(self):
        missing = Path(tempfile.gettempdir()) / 'does-not-exist-fat-test.img'
        if missing.exists():
            missing.unlink()
        self.assertIsNone(read_fat_volume_label(missing))


def _build_fat32_image(
    bpb_label: bytes = b'F32LABEL   ',
    root_entries: list[bytes] | None = None,
) -> bytes:
    """Synthesise a small FAT32 image with a 1-sector cluster size.

    Layout: 32 reserved sectors, 2 FATs of 2 sectors each, root dir in
    cluster 2 (first data cluster).  Cluster count is forced above 65525
    so the image is unambiguously FAT32.
    """
    if root_entries is None:
        root_entries = []
    if len(bpb_label) != 11:
        raise ValueError("bpb_label must be 11 bytes")

    rsvd     = 32
    num_fats = 2
    fat_sz32 = 2
    spc      = 1            # 1 sector per cluster simplifies the math
    tot_sec32 = 70000       # > 65525 clusters → FAT32

    sector = bytearray(BYTES_PER_SECTOR)
    sector[0:3]   = b'\xEB\x58\x90'
    sector[3:11]  = b'MSWIN4.1'
    sector[11:13] = BYTES_PER_SECTOR.to_bytes(2, 'little')
    sector[13]    = spc
    sector[14:16] = rsvd.to_bytes(2, 'little')
    sector[16]    = num_fats
    sector[17:19] = (0).to_bytes(2, 'little')       # BPB_RootEntCnt = 0 on FAT32
    sector[19:21] = (0).to_bytes(2, 'little')       # BPB_TotSec16 = 0 on FAT32
    sector[21]    = 0xF8
    sector[22:24] = (0).to_bytes(2, 'little')       # BPB_FATSz16 = 0 on FAT32
    sector[32:36] = tot_sec32.to_bytes(4, 'little') # BPB_TotSec32
    sector[36:40] = fat_sz32.to_bytes(4, 'little')  # BPB_FATSz32
    sector[44:48] = (2).to_bytes(4, 'little')       # BPB_RootClus
    sector[71:82] = bpb_label                       # BS_VolLab (FAT32)
    sector[82:90] = b'FAT32   '                     # BS_FilSysType
    sector[510:512] = b'\x55\xAA'

    # Root directory = one cluster = spc sectors at first data cluster.
    root_cluster = bytearray(BYTES_PER_SECTOR * spc)
    off = 0
    for entry in root_entries:
        if len(entry) != 32:
            raise ValueError("each root entry must be 32 bytes")
        root_cluster[off:off + 32] = entry
        off += 32

    first_data_sector = rsvd + num_fats * fat_sz32

    image = bytearray()
    image += bytes(sector)
    image += b'\x00' * BYTES_PER_SECTOR * (rsvd - 1)
    image += b'\x00' * BYTES_PER_SECTOR * (num_fats * fat_sz32)
    # image length now equals first_data_sector sectors.
    assert len(image) == first_data_sector * BYTES_PER_SECTOR
    image += bytes(root_cluster)
    consumed = len(image) // BYTES_PER_SECTOR
    image += b'\x00' * BYTES_PER_SECTOR * (tot_sec32 - consumed)
    return bytes(image)


class TestReadFatVolumeLabelFAT32(unittest.TestCase):
    def _write(self, data: bytes) -> Path:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.img')
        tmp.write(data)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_root_dir_label_preferred(self):
        root = _make_dir_entry(b'FAT32ROOT  ', 0x08)
        img = _build_fat32_image(bpb_label=b'BPBNAME    ', root_entries=[root])
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'FAT32ROOT')

    def test_falls_back_to_bs_vollab(self):
        img = _build_fat32_image(bpb_label=b'F32NAME    ')
        path = self._write(img)
        self.assertEqual(read_fat_volume_label(path), 'F32NAME')

    def test_no_name_returns_none(self):
        img = _build_fat32_image(bpb_label=b'NO NAME    ')
        path = self._write(img)
        self.assertIsNone(read_fat_volume_label(path))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
