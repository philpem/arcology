"""
Unit tests for RISC OS INF sidecar file processing.

Tests the INF parser, BBC<->DOS filename translation, and the
process_inf_sidecars() function that renames files and collects metadata.

These functions live in worker/arcworker/tools/extraction.py but are pure
Python with no external tool dependencies, so they can run in CI.

Environment variables:
    WORKER_API_KEY — required by worker config (defaults to 'test')

Run:
    python -m unittest ci.test_inf_processing -v
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


from datetime import datetime

from worker.arcworker.tools.extraction import (
    _parse_inf_line,
    _get_riscos_filetype,
    _riscos_timestamp_to_datetime,
    _translate_filename_bbc_to_dos,
    _translate_filename_dos_to_bbc,
    process_inf_sidecars,
)


# =============================================================================
# _get_riscos_filetype
# =============================================================================

class TestGetRiscosFiletype(unittest.TestCase):
    """Extract RISC OS filetype from a 32-bit load address."""

    def test_date_stamped_text(self):
        # 0xFFFFFFF00 — top 12 bits = 0xFFF, filetype bits 19:8 = 0xFFF
        self.assertEqual(_get_riscos_filetype(0xFFFFFF00), 'fff')

    def test_date_stamped_data(self):
        self.assertEqual(_get_riscos_filetype(0xFFFFFD00), 'ffd')

    def test_date_stamped_basic(self):
        # Filetype 0xFFB (BASIC): load addr with FFB in bits 19:8
        self.assertEqual(_get_riscos_filetype(0xFFFFFB00), 'ffb')

    def test_date_stamped_sprite(self):
        # Filetype 0xFF9 (Sprite): load addr with FF9 in bits 19:8
        self.assertEqual(_get_riscos_filetype(0xFFFFF900), 'ff9')

    def test_date_stamped_low_filetype(self):
        # Filetype 0x001
        self.assertEqual(_get_riscos_filetype(0xFFF00100), '001')

    def test_not_date_stamped(self):
        # Top 12 bits are not 0xFFF — no filetype
        self.assertIsNone(_get_riscos_filetype(0x00FF1900))

    def test_zero(self):
        self.assertIsNone(_get_riscos_filetype(0))

    def test_load_address_boundary(self):
        # 0xFFF00000 — filetype 0x000
        self.assertEqual(_get_riscos_filetype(0xFFF00000), '000')


# =============================================================================
# Filename translation
# =============================================================================

class TestFilenameTranslation(unittest.TestCase):
    """BBC <-> DOS character mapping."""

    def test_bbc_to_dos_hash(self):
        self.assertEqual(_translate_filename_bbc_to_dos('#.BOOT'), '?/BOOT')

    def test_dos_to_bbc_hash(self):
        self.assertEqual(_translate_filename_dos_to_bbc('?/BOOT'), '#.BOOT')

    def test_all_characters(self):
        bbc = '#.$^&@%'
        dos = '?/<>+=;'
        self.assertEqual(_translate_filename_bbc_to_dos(bbc), dos)
        self.assertEqual(_translate_filename_dos_to_bbc(dos), bbc)

    def test_no_special_chars(self):
        self.assertEqual(_translate_filename_bbc_to_dos('README'), 'README')
        self.assertEqual(_translate_filename_dos_to_bbc('README'), 'README')

    def test_roundtrip(self):
        original = '$.!BOOT.#UTIL.@Config'
        dos = _translate_filename_bbc_to_dos(original)
        self.assertEqual(_translate_filename_dos_to_bbc(dos), original)

    def test_mixed_with_normal(self):
        self.assertEqual(_translate_filename_bbc_to_dos('&Run'), '+Run')
        self.assertEqual(_translate_filename_dos_to_bbc('+Run'), '&Run')


# =============================================================================
# _parse_inf_line
# =============================================================================

class TestParseInfLine(unittest.TestCase):
    """Parse a single INF metadata line."""

    def test_basic_three_fields(self):
        result = _parse_inf_line('MYFILE FFFFFD00 FFFFFF00')
        self.assertEqual(result['filename'], 'MYFILE')
        self.assertEqual(result['load_address'], 'fffffd00')
        self.assertEqual(result['exec_address'], 'ffffff00')
        self.assertEqual(result['risc_os_filetype'], 'ffd')

    def test_with_length_and_access_letters(self):
        result = _parse_inf_line('TEST FFFFFD00 FFFFFF00 100 WR/r')
        self.assertEqual(result['attributes'], 'WR/r')
        self.assertEqual(result['risc_os_filetype'], 'ffd')

    def test_with_length_and_access_hex(self):
        result = _parse_inf_line('TEST FFFFFD00 FFFFFF00 100 33')
        self.assertEqual(result['attributes'], '33')

    def test_with_length_only(self):
        result = _parse_inf_line('TEST FFFFFD00 FFFFFF00 100')
        self.assertNotIn('attributes', result)

    def test_locked_file(self):
        result = _parse_inf_line('SAVE FFFFFD00 FFFFFF00 200 L')
        self.assertEqual(result['attributes'], 'L')

    def test_quoted_filename(self):
        result = _parse_inf_line('"MY FILE" FFFFFD00 FFFFFF00')
        self.assertEqual(result['filename'], 'MY FILE')

    def test_quoted_filename_with_spaces_and_fields(self):
        result = _parse_inf_line('"FILE NAME" FFFFFD00 FFFFFF00 50 WR')
        self.assertEqual(result['filename'], 'FILE NAME')
        self.assertEqual(result['attributes'], 'WR')

    def test_non_date_stamped_load_address(self):
        result = _parse_inf_line('LOADER 00001900 00008023')
        self.assertEqual(result['load_address'], '00001900')
        self.assertEqual(result['exec_address'], '00008023')
        self.assertNotIn('risc_os_filetype', result)

    def test_empty_line(self):
        self.assertIsNone(_parse_inf_line(''))

    def test_whitespace_only(self):
        self.assertIsNone(_parse_inf_line('   '))

    def test_too_few_fields(self):
        self.assertIsNone(_parse_inf_line('ONLYONE'))
        self.assertIsNone(_parse_inf_line('TWO FIELDS'))

    def test_invalid_hex(self):
        self.assertIsNone(_parse_inf_line('FILE NOTAHEX FFFFFF00'))

    def test_unterminated_quote(self):
        self.assertIsNone(_parse_inf_line('"UNTERMINATED FFFFFD00 FFFFFF00'))

    def test_case_preserved_in_filename(self):
        result = _parse_inf_line('!Boot FFFFFD00 FFFFFF00')
        self.assertEqual(result['filename'], '!Boot')

    def test_bbc_special_chars_in_filename(self):
        result = _parse_inf_line('#.BOOT FFFFFD00 FFFFFF00')
        self.assertEqual(result['filename'], '#.BOOT')

    def test_addresses_zero_padded_to_8(self):
        result = _parse_inf_line('F 1 2')
        self.assertEqual(result['load_address'], '00000001')
        self.assertEqual(result['exec_address'], '00000002')

    def test_leading_trailing_whitespace(self):
        result = _parse_inf_line('  TEST FFFFFD00 FFFFFF00  ')
        self.assertEqual(result['filename'], 'TEST')


# =============================================================================
# process_inf_sidecars
# =============================================================================

class TestProcessInfSidecars(unittest.TestCase):
    """End-to-end tests for INF sidecar processing."""

    def _make_tree(self, spec: dict[str, str | bytes]) -> Path:
        """Create a temp directory with the given file tree.

        *spec* maps relative paths to content (str for text, bytes for binary).
        Returns the root Path.
        """
        root = Path(tempfile.mkdtemp())
        self._roots.append(root)
        for rel, content in spec.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content)
        return root

    def setUp(self):
        self._roots: list[Path] = []

    def tearDown(self):
        import shutil
        for r in self._roots:
            shutil.rmtree(r, ignore_errors=True)

    # -- DIM-style output: file already named correctly --------------------

    def test_dim_style_no_rename(self):
        """DIM produces correct BBC names with ,xxx suffix; INF adds metadata."""
        root = self._make_tree({
            'TestFile,ffd': b'data',
            'TestFile,ffd.inf': 'TestFile FFFFFD00 FFFFFF00 4 WR',
        })
        meta = process_inf_sidecars(root)

        # File should not be renamed
        self.assertTrue((root / 'TestFile,ffd').exists())
        # INF should be deleted
        self.assertFalse((root / 'TestFile,ffd.inf').exists())
        # Metadata keyed by display path (suffix stripped)
        self.assertIn('TestFile', meta)
        self.assertEqual(meta['TestFile']['load_address'], 'fffffd00')
        self.assertEqual(meta['TestFile']['exec_address'], 'ffffff00')
        self.assertEqual(meta['TestFile']['risc_os_filetype'], 'ffd')
        self.assertEqual(meta['TestFile']['attributes'], 'WR')

    # -- DOS-encoded filename translation ----------------------------------

    def test_dos_to_bbc_rename(self):
        """File with DOS-encoded ? gets renamed to BBC #."""
        root = self._make_tree({
            '?BOOT': b'data',
            '?BOOT.inf': '#BOOT FFFFFD00 FFFFFF00 4 L',
        })
        meta = process_inf_sidecars(root)

        self.assertTrue((root / '#BOOT').exists())
        self.assertFalse((root / '?BOOT').exists())
        self.assertIn('#BOOT', meta)
        self.assertEqual(meta['#BOOT']['attributes'], 'L')

    def test_dos_plus_to_bbc_ampersand(self):
        """DOS + renamed to BBC &."""
        root = self._make_tree({
            '+Run,feb': b'run',
            '+Run,feb.inf': '&Run FFFFFEB0 FFFFFF00 3 WR',
        })
        meta = process_inf_sidecars(root)

        self.assertTrue((root / '&Run,feb').exists())
        self.assertFalse((root / '+Run,feb').exists())
        # Display path strips the ,xxx suffix
        self.assertIn('&Run', meta)
        self.assertEqual(meta['&Run']['risc_os_filetype'], 'ffe')

    def test_dos_equals_to_bbc_at(self):
        """DOS = renamed to BBC @."""
        root = self._make_tree({
            '=Config': b'cfg',
            '=Config.inf': '@Config FF000000 FF000000 3',
        })
        meta = process_inf_sidecars(root)

        self.assertTrue((root / '@Config').exists())
        self.assertFalse((root / '=Config').exists())

    # -- No rename needed when name already correct ------------------------

    def test_already_bbc_name_no_rename(self):
        """File already has BBC characters — no rename needed."""
        root = self._make_tree({
            '&Run,feb': b'run',
            '&Run,feb.inf': '&Run FFFFFEB0 FFFFFF00 3 WR',
        })
        meta = process_inf_sidecars(root)

        self.assertTrue((root / '&Run,feb').exists())
        self.assertIn('&Run', meta)

    # -- INF with no data file is skipped ----------------------------------

    def test_orphan_inf_skipped(self):
        """INF with no matching data file is left untouched."""
        root = self._make_tree({
            'orphan.inf': 'ORPHAN FFFFFD00 FFFFFF00 5',
        })
        meta = process_inf_sidecars(root)

        self.assertEqual(meta, {})
        self.assertTrue((root / 'orphan.inf').exists())

    # -- Case-insensitive .inf extension -----------------------------------

    def test_uppercase_inf_extension(self):
        """Uppercase .INF is also processed."""
        root = self._make_tree({
            'MyFile,ffd': b'data',
            'MyFile,ffd.INF': 'MyFile FFFFFD00 FFFFFF00 4',
        })
        meta = process_inf_sidecars(root)

        self.assertFalse((root / 'MyFile,ffd.INF').exists())
        self.assertIn('MyFile', meta)

    def test_mixed_case_inf_extension(self):
        """Mixed case .Inf is also processed."""
        root = self._make_tree({
            'Readme,fff': b'text',
            'Readme,fff.Inf': 'Readme FFFFFF00 FFFFFF00 4 WR',
        })
        meta = process_inf_sidecars(root)

        self.assertFalse((root / 'Readme,fff.Inf').exists())
        self.assertIn('Readme', meta)

    # -- Subdirectories ----------------------------------------------------

    def test_subdirectory_inf(self):
        """INF files in subdirectories are processed."""
        root = self._make_tree({
            'subdir/readme,fff': b'text',
            'subdir/readme,fff.inf': 'readme FFFFFF00 FFFFFF00 4 WR',
        })
        meta = process_inf_sidecars(root)

        self.assertFalse((root / 'subdir' / 'readme,fff.inf').exists())
        self.assertIn('subdir/readme', meta)
        self.assertEqual(meta['subdir/readme']['risc_os_filetype'], 'fff')

    def test_nested_subdirectories(self):
        """INF files at multiple nesting levels."""
        root = self._make_tree({
            'a/b/file,ffd': b'deep',
            'a/b/file,ffd.inf': 'file FFFFFD00 FFFFFF00 4',
            'a/top,fff': b'shallow',
            'a/top,fff.inf': 'top FFFFFF00 FFFFFF00 7',
        })
        meta = process_inf_sidecars(root)

        self.assertEqual(len(meta), 2)
        self.assertIn('a/b/file', meta)
        self.assertIn('a/top', meta)

    # -- Rename conflict ---------------------------------------------------

    def test_rename_conflict_skips_rename(self):
        """If the target name already exists, skip rename but keep metadata."""
        root = self._make_tree({
            '#BOOT': b'original',     # Target name already exists
            '?BOOT': b'dos-encoded',  # Would rename to #BOOT
            '?BOOT.inf': '#BOOT FFFFFD00 FFFFFF00 5',
        })
        meta = process_inf_sidecars(root)

        # Both files should still exist (rename skipped due to conflict)
        self.assertTrue((root / '#BOOT').exists())
        self.assertTrue((root / '?BOOT').exists())
        # INF still deleted, metadata still collected (keyed by current name)
        self.assertFalse((root / '?BOOT.inf').exists())

    # -- Non-date-stamped addresses ----------------------------------------

    def test_non_date_stamped_no_filetype(self):
        """Load addresses without 0xFFF prefix produce no filetype."""
        root = self._make_tree({
            'LOADER': b'code',
            'LOADER.inf': 'LOADER 00001900 00008023 100',
        })
        meta = process_inf_sidecars(root)

        self.assertIn('LOADER', meta)
        self.assertEqual(meta['LOADER']['load_address'], '00001900')
        self.assertEqual(meta['LOADER']['exec_address'], '00008023')
        self.assertNotIn('risc_os_filetype', meta['LOADER'])

    # -- Empty directory returns empty dict --------------------------------

    def test_empty_directory(self):
        root = self._make_tree({})
        meta = process_inf_sidecars(root)
        self.assertEqual(meta, {})


# =============================================================================
# _riscos_timestamp_to_datetime
# =============================================================================

class TestRiscosTimestampToDatetime(unittest.TestCase):
    """Decode RISC OS 5-byte date-stamp to UTC datetime."""

    def _make_load_exec(self, filetype: int, cs: int) -> tuple[int, int]:
        """Build load/exec address pair for a given filetype and centisecond count."""
        high = (cs >> 32) & 0xFF
        load = 0xFFF00000 | ((filetype & 0xFFF) << 8) | high
        exec_ = cs & 0xFFFFFFFF
        return load, exec_

    def test_not_date_stamped_returns_none(self):
        """Load address without 0xFFF prefix gives None."""
        self.assertIsNone(_riscos_timestamp_to_datetime(0x00001900, 0x00008023))

    def test_zero_load_addr_returns_none(self):
        self.assertIsNone(_riscos_timestamp_to_datetime(0, 0))

    def test_risc_os_epoch(self):
        """cs=0 corresponds to the RISC OS epoch: 1900-01-01 00:00:00."""
        # load_addr with FFF prefix, FD filetype, low byte 0x00; exec = 0
        result = _riscos_timestamp_to_datetime(0xFFFFFD00, 0x00000000)
        self.assertEqual(result, datetime(1900, 1, 1, 0, 0, 0))

    def test_unix_epoch(self):
        """cs=220898880000 → 1970-01-01 00:00:00 UTC."""
        cs = 220898880000  # seconds from 1900-01-01 to 1970-01-01 * 100
        load, exec_ = self._make_load_exec(0xFFD, cs)
        result = _riscos_timestamp_to_datetime(load, exec_)
        self.assertEqual(result, datetime(1970, 1, 1, 0, 0, 0))

    def test_known_date_roundtrip(self):
        """Round-trip: encode a known datetime, decode it back."""
        import calendar
        expected = datetime(1994, 10, 1, 0, 0, 0)
        # RISC OS centiseconds = Unix seconds * 100 + epoch offset
        cs = calendar.timegm(expected.timetuple()) * 100 + 220898880000
        load, exec_ = self._make_load_exec(0xFFD, cs)
        result = _riscos_timestamp_to_datetime(load, exec_)
        self.assertEqual(result, expected)

    def test_returns_naive_datetime(self):
        """Returned datetime is naive (no tzinfo attached)."""
        result = _riscos_timestamp_to_datetime(0xFFFFFD00, 0x00000000)
        self.assertIsNotNone(result)
        self.assertIsNone(result.tzinfo)

    def test_different_filetype_same_timestamp(self):
        """Filetype bits do not affect the decoded timestamp."""
        import calendar
        dt = datetime(1990, 6, 15, 12, 0, 0)
        cs = calendar.timegm(dt.timetuple()) * 100 + 220898880000
        load_ffd, exec_ = self._make_load_exec(0xFFD, cs)
        load_fff, _    = self._make_load_exec(0xFFF, cs)
        self.assertEqual(
            _riscos_timestamp_to_datetime(load_ffd, exec_),
            _riscos_timestamp_to_datetime(load_fff, exec_),
        )


# =============================================================================
# _parse_inf_line — timestamp fields
# =============================================================================

class TestParseInfLineTimestamp(unittest.TestCase):
    """Verify _parse_inf_line() emits modified_time for date-stamped files."""

    def test_date_stamped_has_modified_time(self):
        """Date-stamped load address → modified_time included."""
        result = _parse_inf_line('MYFILE FFFFFD00 FFFFFF00')
        self.assertIn('modified_time', result)

    def test_non_date_stamped_no_modified_time(self):
        """Non-date-stamped load address → no modified_time."""
        result = _parse_inf_line('LOADER 00001900 00008023')
        self.assertNotIn('modified_time', result)

    def test_modified_time_parses_as_iso_datetime(self):
        """modified_time value is a valid ISO 8601 datetime string."""
        result = _parse_inf_line('MYFILE FFFFFD00 FFFFFF00')
        ts_str = result['modified_time']
        # Should not raise
        dt = datetime.fromisoformat(ts_str)
        self.assertIsInstance(dt, datetime)

    def test_modified_time_matches_decoder(self):
        """modified_time agrees with _riscos_timestamp_to_datetime."""
        load_hex, exec_hex = 'FFFFFD00', 'FFFFFF00'
        result = _parse_inf_line(f'F {load_hex} {exec_hex}')
        expected = _riscos_timestamp_to_datetime(
            int(load_hex, 16), int(exec_hex, 16)
        )
        self.assertEqual(datetime.fromisoformat(result['modified_time']), expected)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
