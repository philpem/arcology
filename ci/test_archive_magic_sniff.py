"""
Tests for magic-byte archive sniffing.

Verifies that _sniff_archive_magic correctly identifies Spark and ArcFS
archives even when they have a .zip extension, matching the behaviour
described in issue #446.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.archive_formats import ArchiveType
from worker.arcworker.analyses.extraction import _sniff_archive_magic


class TestSniffArchiveMagic(unittest.TestCase):

    def _write(self, data: bytes) -> Path:
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        fh.write(data)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return Path(fh.name)

    # ── Spark ──────────────────────────────────────────────────────────

    def test_spark_stored_entry(self):
        """0x1A 0x01 — Spark stored file."""
        self.assertEqual(_sniff_archive_magic(self._write(b'\x1a\x01rest')), ArchiveType.SPARK)

    def test_spark_end_of_archive(self):
        """0x1A 0x00 — Spark end-of-archive marker."""
        self.assertEqual(_sniff_archive_magic(self._write(b'\x1a\x00rest')), ArchiveType.SPARK)

    def test_spark_squash_compat(self):
        """0x1A 0x82 — Spark/Squash compatibility method byte."""
        self.assertEqual(_sniff_archive_magic(self._write(b'\x1a\x82rest')), ArchiveType.SPARK)

    def test_spark_directory(self):
        """0x1A 0xFF — Spark directory entry."""
        self.assertEqual(_sniff_archive_magic(self._write(b'\x1a\xff rest')), ArchiveType.SPARK)

    # ── ArcFS ──────────────────────────────────────────────────────────

    def test_arcfs_archive_null(self):
        """b'Archive\\0' — ArcFS header."""
        self.assertEqual(_sniff_archive_magic(self._write(b'Archive\x00')), ArchiveType.ARCFS)

    def test_arcfs_lowercase(self):
        """b'\\x1aarchive' — ArcFS alternate header."""
        self.assertEqual(_sniff_archive_magic(self._write(b'\x1aarchive')), ArchiveType.ARCFS)

    # ── ZIP ────────────────────────────────────────────────────────────

    def test_zip_pk_header(self):
        """PK\\x03\\x04 — standard ZIP local file header."""
        self.assertEqual(_sniff_archive_magic(self._write(b'PK\x03\x04rest')), ArchiveType.ZIP)

    # ── Unrecognised ───────────────────────────────────────────────────

    def test_unknown_returns_none(self):
        """Unknown magic bytes return None."""
        self.assertIsNone(_sniff_archive_magic(self._write(b'\xff\xfe\xfd\xfc')))

    def test_too_short_returns_none(self):
        """Single-byte file returns None."""
        self.assertIsNone(_sniff_archive_magic(self._write(b'\x1a')))

    def test_nonexistent_file_returns_none(self):
        """Missing file returns None without raising."""
        self.assertIsNone(_sniff_archive_magic(Path('/tmp/no_such_file_446.zip')))

    # ── Mis-labelled: Spark named .zip ─────────────────────────────────

    def test_spark_named_zip_detected_correctly(self):
        """A Spark archive distributed with a .zip extension is identified as SPARK, not ZIP.

        This is the core scenario from issue #446: RISC OS FTP sites often
        distributed Spark archives renamed to .zip for cross-platform compatibility.
        """
        spark_bytes = b'\x1a\x82' + b'\x00' * 30   # Spark squash-compat header
        self.assertEqual(_sniff_archive_magic(self._write(spark_bytes)), ArchiveType.SPARK)

    def test_arcfs_named_zip_detected_correctly(self):
        """An ArcFS archive distributed with a .zip extension is identified as ARCFS."""
        arcfs_bytes = b'Archive\x00' + b'\x00' * 20
        self.assertEqual(_sniff_archive_magic(self._write(arcfs_bytes)), ArchiveType.ARCFS)


if __name__ == '__main__':
    unittest.main()
# vim: ts=4 sw=4 et
