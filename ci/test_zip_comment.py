"""Tests for read_zip_comment(): pure-Python ZIP archive-comment extractor."""

import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.tools.archives import read_zip_comment


def _build_zip_with_comment(path: Path, files: dict[str, bytes], comment: bytes) -> None:
    """Build a small ZIP and append a custom EOCD comment block."""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.comment = comment


class TestReadZipComment(unittest.TestCase):
    def test_no_comment_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
            zp = Path(t.name)
        try:
            _build_zip_with_comment(zp, {'a.txt': b'hello'}, b'')
            self.assertIsNone(read_zip_comment(zp))
        finally:
            zp.unlink()

    def test_ascii_comment(self):
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
            zp = Path(t.name)
        try:
            _build_zip_with_comment(zp, {'a.txt': b'hello'}, b'Hello world!')
            self.assertEqual(read_zip_comment(zp), 'Hello world!')
        finally:
            zp.unlink()

    def test_cp437_box_drawing_decodes(self):
        """The example in issue #93 uses CP437 box-drawing characters."""
        # CP437 byte 0xCD = '═', 0xBB = '╗', 0xBA = '║', 0xC8 = '╚', 0xBC = '╝', 0xC9 = '╔'
        cp437_bytes = bytes([0xC9]) + bytes([0xCD]) * 3 + bytes([0xBB])
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
            zp = Path(t.name)
        try:
            _build_zip_with_comment(zp, {'a.txt': b'x'}, cp437_bytes)
            decoded = read_zip_comment(zp)
            self.assertEqual(decoded, '╔═══╗')
        finally:
            zp.unlink()

    def test_multiline_comment_preserved(self):
        comment = b'Line 1\r\nLine 2\r\nLine 3'
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
            zp = Path(t.name)
        try:
            _build_zip_with_comment(zp, {'a.txt': b'x'}, comment)
            decoded = read_zip_comment(zp)
            self.assertIn('Line 1', decoded)
            self.assertIn('Line 2', decoded)
            self.assertIn('Line 3', decoded)
        finally:
            zp.unlink()

    def test_non_zip_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as t:
            t.write(b'not a zip file at all')
            bp = Path(t.name)
        try:
            self.assertIsNone(read_zip_comment(bp))
        finally:
            bp.unlink()

    def test_truncated_eocd_returns_none(self):
        """A file containing the EOCD signature but not enough bytes after it."""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
            t.write(b'PK\x05\x06' + b'\x00' * 5)  # signature plus partial EOCD
            zp = Path(t.name)
        try:
            self.assertIsNone(read_zip_comment(zp))
        finally:
            zp.unlink()

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_zip_comment(Path('/nonexistent/path/foo.zip')))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
