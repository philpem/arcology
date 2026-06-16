"""
Regression tests for bounded-memory file access in the worker.

A 6 GB ADFS hard-disc image once OOM-killed a worker during PARTITION_DETECT:
the inline ARMlock probe (`detect_armlock`) did `bytearray(path.read_bytes())`,
loading the whole image into RAM.  The worker now reads images with bounded
seek/read access (`SectorReader`) and caps the whole-file reads of the
small-file parsers (`read_file_capped`).

These tests assert those helpers' semantics and that `detect_armlock` no longer
scales its memory with image size.

Run:
    python -m unittest ci.test_worker_io -v
"""

import os
import resource
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools import fs_riscos_armlock as armlock  # noqa: E402
from worker.arcworker.tools.base import (  # noqa: E402
    FileTooLargeError,
    open_sector_reader,
    read_file_capped,
)
from worker.arcworker.tools.fs_riscos_armlock import detect_armlock  # noqa: E402


def _build_filecore_dir() -> bytes:
    """Build a minimal new-format ('Hugo') FileCore directory with one file."""
    block = bytearray(armlock.FILECORE_DIR_SIZE)
    block[1:armlock.FILECORE_DIR_HEADER_SIZE] = b'Hugo'
    e = armlock.FILECORE_DIR_HEADER_SIZE
    block[e:e + 5] = b'FILE\r'                                  # CR-terminated name
    struct.pack_into('<I', block, e + armlock._DIRENT_LOAD, 0xFFFFFD00)
    struct.pack_into('<I', block, e + armlock._DIRENT_EXEC, 0)
    struct.pack_into('<I', block, e + armlock._DIRENT_LENGTH, 0x1234)
    block[e + armlock._DIRENT_SIN:e + armlock._DIRENT_SIN + 3] = (0x300).to_bytes(3, 'little')
    block[e + armlock._DIRENT_ATTR] = 0x00                      # not a directory
    # Next entry's name byte 0x00 terminates the list (already zero).
    return bytes(block)


def _make_sparse(size: int) -> Path:
    """Create a sparse file of *size* bytes; return its path."""
    fd, name = tempfile.mkstemp()
    os.close(fd)
    p = Path(name)
    with open(p, 'wb') as f:
        f.seek(size - 1)
        f.write(b'\0')
    return p


class TestSectorReader(unittest.TestCase):
    def test_empty_file(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            with open_sector_reader(p) as buf:
                self.assertEqual(len(buf), 0)
                self.assertEqual(buf[0:4], b'')
        finally:
            os.unlink(p)

    def test_indexing_and_slicing_match_contents(self):
        payload = bytes(range(256)) * 4
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            p.write_bytes(payload)
            with open_sector_reader(p) as buf:
                self.assertEqual(len(buf), len(payload))
                self.assertEqual(buf[0], payload[0])
                self.assertEqual(buf[100], payload[100])
                self.assertEqual(buf[-1], payload[-1])
                self.assertEqual(bytes(buf[10:20]), payload[10:20])
                # The materialised slice supports the buffer protocol that the
                # FileCore parsers need for struct.unpack_from.
                self.assertEqual(
                    struct.unpack_from('<I', buf[16:20], 0)[0],
                    struct.unpack_from('<I', payload, 16)[0],
                )
        finally:
            os.unlink(p)

    def test_out_of_range_index_raises(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            p.write_bytes(b'abc')
            with open_sector_reader(p) as buf:
                with self.assertRaises(IndexError):
                    buf[3]
        finally:
            os.unlink(p)


class TestParseDirectoryOverSectorReader(unittest.TestCase):
    """_parse_directory must give identical results over bytes and a SectorReader.

    Guards the refactor that materialises the directory block once (so
    struct.unpack_from works) and lets the FileCore parsers run over seek/read
    rather than a whole-image buffer.
    """

    def _assert_one_file(self, valid, entries):
        self.assertTrue(valid)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'FILE')
        self.assertEqual(entries[0]['length'], 0x1234)
        self.assertEqual(entries[0]['sin'], 0x300)
        self.assertFalse(entries[0]['is_dir'])

    def test_bytes_and_sector_reader_agree(self):
        block = _build_filecore_dir()
        self._assert_one_file(*armlock._parse_directory(block, 0))

        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            # Place the directory at a non-zero offset to exercise seeking.
            p.write_bytes(b'\x00' * 4096 + block)
            with open_sector_reader(p) as buf:
                self._assert_one_file(*armlock._parse_directory(buf, 4096))
        finally:
            os.unlink(p)


class TestReadFileCapped(unittest.TestCase):
    def test_returns_contents_under_cap(self):
        payload = b'hello world' * 10
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            p.write_bytes(payload)
            self.assertEqual(read_file_capped(p, max_bytes=10_000), payload)
        finally:
            os.unlink(p)

    def test_refuses_file_over_cap_without_reading_it(self):
        # 1 GiB sparse file with a tiny cap: must raise *before* reading, so no
        # gigabyte is pulled into RAM (FileTooLargeError is an OSError, which the
        # small-file parsers already handle as "unreadable").
        p = _make_sparse(1024**3)
        if p.stat().st_blocks * 512 > 64 * 1024**2:
            os.unlink(p)
            self.skipTest('filesystem did not create a sparse file')
        try:
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            with self.assertRaises(FileTooLargeError):
                read_file_capped(p, max_bytes=1024 * 1024)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            self.assertIsInstance(FileTooLargeError(), OSError)
            self.assertLess((after - before) * 1024, 256 * 1024**2)
        finally:
            os.unlink(p)


class TestDetectArmlockMemory(unittest.TestCase):
    def test_large_image_is_not_loaded_into_ram(self):
        # 2 GiB sparse image: the pre-fix code allocated this whole size as a
        # bytearray; the SectorReader path touches only a few sectors.
        size = 2 * 1024**3
        p = _make_sparse(size)
        # Skip on the rare filesystem that doesn't honour sparse allocation,
        # so we don't actually write 2 GiB.
        if p.stat().st_blocks * 512 > 64 * 1024**2:
            os.unlink(p)
            self.skipTest('filesystem did not create a sparse file')
        try:
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result = detect_armlock(p)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Returns cleanly (all-zero image is not ARMlock) without OOM.
            self.assertFalse(result.get('detected'))
            # Peak RSS must not grow anywhere near the 2 GiB file size.
            self.assertLess((after - before) * 1024, 512 * 1024**2)
        finally:
            os.unlink(p)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
