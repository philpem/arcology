"""
Tests for RISC OS Latin-1 filename encoding helpers.

Covers:
  - decode_riscos_latin1: byte-level decoding of RISC OS Latin-1 characters
  - fix_riscos_c1_filenames: post-extraction rename of ISO 8859-1 C1 chars
  - has_riscos_zip_metadata: detection of Acorn extra-field (0x4341) in ZIPs

No Flask app context, Docker, or external tools required.

Run:
    python -m unittest ci.test_riscos_encoding -v
"""

import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools.archives import has_riscos_zip_metadata
from worker.arcworker.utils.text import decode_riscos_latin1, fix_riscos_c1_filenames

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zip_bytes(entries: list[tuple[str, bytes, bool]]) -> bytes:
    """Build a minimal valid ZIP from a list of (filename_bytes, data, acorn_extra) tuples.

    *filename_bytes* is the raw bytes stored in the ZIP filename field.
    *data* is the file content (may be empty).
    *acorn_extra* controls whether an Acorn/SparkFS extra-field block
    (ID 0x4341) is added to the central directory entry.

    The local file headers use no extra field; the central directory entries
    optionally carry the Acorn block so that has_riscos_zip_metadata() can
    detect it.
    """
    # Acorn extra-field block: 2-byte ID (little-endian 0x4341 = "AC") +
    # 2-byte data length (20 bytes) + 20 zero bytes of payload.
    _ACORN_EXTRA = b'\x41\x43' + struct.pack('<H', 20) + b'\x00' * 20

    local_parts = []
    cd_parts = []
    offset = 0

    for raw_fname, file_data, want_acorn in entries:
        crc = zlib.crc32(file_data) & 0xFFFFFFFF
        flen = len(raw_fname)
        dlen = len(file_data)

        # Local file header (no extra field)
        local_hdr = struct.pack(
            '<4sHHHHHIIIHH',
            b'PK\x03\x04',
            20,    # version needed
            0,     # general purpose bit flag
            0,     # compression method (stored)
            0, 0,  # mod time, mod date
            crc, dlen, dlen,
            flen, 0,  # filename length, extra length
        ) + raw_fname + file_data
        local_parts.append(local_hdr)

        # Central directory entry (optionally with Acorn extra field)
        extra = _ACORN_EXTRA if want_acorn else b''
        cd_entry = struct.pack(
            '<4sHHHHHHIIIHHHHHII',
            b'PK\x01\x02',
            0x031E, 20,  # version made by, version needed
            0, 0, 0,     # flags, compression, mod time
            0,           # mod date
            crc, dlen, dlen,
            flen, len(extra), 0,  # fname len, extra len, comment len
            0, 0,        # disk start, internal attrs
            0o100644 << 16,  # external attrs
            offset,      # relative offset of local header
        ) + raw_fname + extra
        cd_parts.append(cd_entry)
        offset += len(local_hdr)

    cd_data = b''.join(cd_parts)
    cd_offset = offset
    eocd = struct.pack(
        '<4sHHHHIIH',
        b'PK\x05\x06',
        0, 0,  # disk number, disk with CD
        len(entries), len(entries),
        len(cd_data), cd_offset,
        0,  # comment length
    )
    return b''.join(local_parts) + cd_data + eocd


# ---------------------------------------------------------------------------
# decode_riscos_latin1
# ---------------------------------------------------------------------------

class TestDecodeRiscosLatin1(unittest.TestCase):
    """Unit tests for the RISC OS Latin-1 byte decoder."""

    def test_ascii_passthrough(self):
        """Pure ASCII bytes decode unchanged."""
        self.assertEqual(decode_riscos_latin1(b'Hello'), 'Hello')

    def test_hard_space(self):
        """Byte 0xA0 (RISC OS hard space) decodes to U+00A0 (NBSP)."""
        result = decode_riscos_latin1(b'RISC\xa0OS')
        self.assertEqual(result, 'RISC\u00a0OS')
        self.assertEqual(ord(result[4]), 0xA0)

    def test_upper_latin1_range(self):
        """Bytes 0xA1–0xFF match ISO 8859-1 (same as RISC OS for this range)."""
        for b in range(0xA1, 0x100):
            result = decode_riscos_latin1(bytes([b]))
            self.assertEqual(result, chr(b), f'byte 0x{b:02X}')

    def test_c1_range_remapped(self):
        """Bytes 0x80–0x9F are remapped to RISC OS printable characters, not C1 controls."""
        from worker.arcworker.utils.text import _RISCOS_C1
        for b in range(0x80, 0xA0):
            result = decode_riscos_latin1(bytes([b]))
            expected = _RISCOS_C1[b]
            self.assertEqual(result, expected, f'byte 0x{b:02X}')
            # Must NOT be a C1 control code
            self.assertFalse(
                0x80 <= ord(result) <= 0x9F,
                f'byte 0x{b:02X} decoded to C1 control U+{ord(result):04X}',
            )

    def test_mixed_string(self):
        """Mixed ASCII and non-ASCII bytes decode correctly."""
        # "PD Libs" with 0xA0 hard space
        result = decode_riscos_latin1(b'PD\xa0Libs')
        self.assertEqual(result, 'PD\u00a0Libs')

    def test_empty(self):
        self.assertEqual(decode_riscos_latin1(b''), '')


# ---------------------------------------------------------------------------
# fix_riscos_c1_filenames
# ---------------------------------------------------------------------------

class TestFixRiscosC1Filenames(unittest.TestCase):
    """Tests for the post-extraction C1 control-code remapping function."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_c1_char_in_filename_remapped(self):
        """A file whose name contains a C1 control code (e.g. U+008C) is renamed."""
        from worker.arcworker.utils.text import _RISCOS_C1
        # U+008C is the ISO 8859-1 C1 code for byte 0x8C; in RISC OS that byte
        # is '…' (ellipsis).
        bad_name = 'File\u008cName'
        good_name = 'File' + _RISCOS_C1[0x8C] + 'Name'
        (self.root / bad_name).write_text('content')

        fix_riscos_c1_filenames(self.root)

        self.assertFalse((self.root / bad_name).exists(), 'old name should be gone')
        self.assertTrue((self.root / good_name).exists(), 'new name should exist')

    def test_ascii_only_unchanged(self):
        """Pure ASCII filenames are left untouched."""
        (self.root / 'ReadMe,fff').write_text('content')
        fix_riscos_c1_filenames(self.root)
        self.assertTrue((self.root / 'ReadMe,fff').exists())

    def test_nbsp_unchanged(self):
        """U+00A0 (NBSP, byte 0xA0) is NOT in the C1 range and is left alone."""
        name = 'RISC\u00a0OS,fff'
        (self.root / name).write_text('content')
        fix_riscos_c1_filenames(self.root)
        self.assertTrue((self.root / name).exists(), 'NBSP filename should be preserved')

    def test_directory_renamed(self):
        """Directories with C1 chars in their names are also renamed."""
        from worker.arcworker.utils.text import _RISCOS_C1
        bad_dir = self.root / ('Dir\u0081Sub')
        bad_dir.mkdir()
        (bad_dir / 'file.txt').write_text('hi')

        fix_riscos_c1_filenames(self.root)

        good_dir_name = 'Dir' + _RISCOS_C1[0x81] + 'Sub'
        self.assertFalse(bad_dir.exists())
        self.assertTrue((self.root / good_dir_name).exists())
        self.assertTrue((self.root / good_dir_name / 'file.txt').exists())

    def test_target_exists_skips_rename(self):
        """If the target name already exists, the rename is skipped gracefully."""
        from worker.arcworker.utils.text import _RISCOS_C1
        bad_name = 'File\u0080'
        good_name = 'File' + _RISCOS_C1[0x80]
        (self.root / bad_name).write_text('old')
        (self.root / good_name).write_text('existing')

        fix_riscos_c1_filenames(self.root)  # must not raise

        # Both files still exist (rename was skipped)
        self.assertTrue((self.root / bad_name).exists())
        self.assertTrue((self.root / good_name).exists())


# ---------------------------------------------------------------------------
# has_riscos_zip_metadata
# ---------------------------------------------------------------------------

class TestHasRiscosZipMetadata(unittest.TestCase):
    """Tests for the Acorn extra-field detector."""

    def _write_zip(self, entries, suffix='.zip'):
        """Write ZIP bytes to a temp file and return its path."""
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            f.write(_make_zip_bytes(entries))
            f.flush()
            return Path(f.name)
        finally:
            f.close()

    def test_detects_acorn_extra_field(self):
        """Returns True when a central-directory entry has the 0x4341 block."""
        zip_path = self._write_zip([
            (b'ReadMe,fff', b'hello', True),
        ])
        try:
            self.assertTrue(has_riscos_zip_metadata(zip_path))
        finally:
            zip_path.unlink(missing_ok=True)

    def test_plain_zip_returns_false(self):
        """Returns False for a plain ZIP with no Acorn extra field."""
        zip_path = self._write_zip([
            (b'readme.txt', b'hello', False),
            (b'data.bin', b'\x00\x01\x02', False),
        ])
        try:
            self.assertFalse(has_riscos_zip_metadata(zip_path))
        finally:
            zip_path.unlink(missing_ok=True)

    def test_mixed_entries_returns_true(self):
        """Returns True if ANY entry has the Acorn block (first entry doesn't)."""
        zip_path = self._write_zip([
            (b'plain.txt', b'no acorn here', False),
            (b'RISC\xa0OS,fff', b'acorn file', True),
        ])
        try:
            self.assertTrue(has_riscos_zip_metadata(zip_path))
        finally:
            zip_path.unlink(missing_ok=True)

    def test_nonexistent_file_returns_false(self):
        """Returns False gracefully for a missing file."""
        self.assertFalse(has_riscos_zip_metadata(Path('/nonexistent/file.zip')))

    def test_empty_file_returns_false(self):
        """Returns False gracefully for a zero-byte file."""
        f = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
        f.close()
        try:
            self.assertFalse(has_riscos_zip_metadata(Path(f.name)))
        finally:
            os.unlink(f.name)

    def test_not_a_zip_returns_false(self):
        """Returns False gracefully for a non-ZIP file."""
        f = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
        f.write(b'this is not a zip file at all')
        f.close()
        try:
            self.assertFalse(has_riscos_zip_metadata(Path(f.name)))
        finally:
            os.unlink(f.name)


# ---------------------------------------------------------------------------
# Integration: decode_riscos_latin1 correctly maps all 256 bytes
# ---------------------------------------------------------------------------

class TestDecodeRiscosLatin1AllBytes(unittest.TestCase):
    """Exhaustive table check: no byte should decode to a C1 control code."""

    def test_no_output_is_c1_control(self):
        """None of the 256 bytes should produce a C1 control character in the output."""
        for b in range(256):
            result = decode_riscos_latin1(bytes([b]))
            cp = ord(result)
            self.assertFalse(
                0x80 <= cp <= 0x9F,
                f'byte 0x{b:02X} decoded to C1 control U+{cp:04X}',
            )

    def test_ascii_identity(self):
        """All bytes 0x00–0x7F decode to the corresponding ASCII character."""
        for b in range(0x80):
            self.assertEqual(decode_riscos_latin1(bytes([b])), chr(b))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
