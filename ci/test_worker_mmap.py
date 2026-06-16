"""
Regression tests for bounded-memory image access in the worker.

A 6 GB ADFS hard-disc image once OOM-killed a worker during PARTITION_DETECT:
the inline ARMlock probe (`detect_armlock`) did `bytearray(path.read_bytes())`,
loading the whole image into RAM.  The fix is `mmap_readonly()` (random access
with only touched pages resident) plus a seek/read/write `remove_armlock`.

These tests assert the helper's semantics and that `detect_armlock` no longer
scales its memory with image size.

Run:
    python -m unittest ci.test_worker_mmap -v
"""

import os
import resource
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools.base import (  # noqa: E402
    FileTooLargeError,
    mmap_readonly,
    read_file_capped,
)
from worker.arcworker.tools.fs_riscos_armlock import detect_armlock  # noqa: E402


def _make_sparse(size: int) -> Path:
    """Create a sparse file of *size* bytes; return its path."""
    fd, name = tempfile.mkstemp()
    os.close(fd)
    p = Path(name)
    with open(p, 'wb') as f:
        f.seek(size - 1)
        f.write(b'\0')
    return p


class TestMmapReadonly(unittest.TestCase):
    def test_empty_file_yields_empty_bytes(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            with mmap_readonly(p) as buf:
                self.assertEqual(len(buf), 0)
                self.assertEqual(buf[:], b'')
        finally:
            os.unlink(p)

    def test_indexing_and_slicing_match_contents(self):
        payload = bytes(range(256)) * 4
        fd, name = tempfile.mkstemp()
        os.close(fd)
        p = Path(name)
        try:
            p.write_bytes(payload)
            with mmap_readonly(p) as buf:
                self.assertEqual(len(buf), len(payload))
                self.assertEqual(buf[0], payload[0])
                self.assertEqual(buf[100], payload[100])
                self.assertEqual(bytes(buf[10:20]), payload[10:20])
                # struct-style buffer-protocol access must work (parsers rely on it)
                self.assertEqual(memoryview(buf)[5], payload[5])
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
        # bytearray; the mmap path touches only a few low pages.
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
