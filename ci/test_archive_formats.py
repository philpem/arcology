"""
Unit tests for shared/archive_formats.py.

Checks that the ARCHIVE_FORMATS dict is internally consistent: every
ArchiveType enum member has an entry, required fields are present, and
the helper functions behave correctly.

shared/archive_formats.py has no external dependencies, so these tests
run without a Flask app context (though they are grouped in the app-tests
CI job for simplicity).

Run:
    python -m unittest ci.test_archive_formats -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.archive_formats import (
    ArchiveType,
    ArchiveCategory,
    ARCHIVE_FORMATS,
    get_archive_info,
    get_archive_by_filetype,
    get_archive_by_extension,
    is_archive_format,
    is_compressor_format,
    is_disk_image_format,
)

_REQUIRED_FIELDS = ('name', 'category', 'extensions', 'description', 'tool', 'extract_creates_dir')


class TestArchiveFormatsCompleteness(unittest.TestCase):
    """Every ArchiveType member must have a corresponding ARCHIVE_FORMATS entry."""

    def test_all_archive_types_have_format_entry(self):
        missing = set(ArchiveType) - set(ARCHIVE_FORMATS.keys())
        self.assertFalse(
            missing,
            f'ArchiveType members without an ARCHIVE_FORMATS entry: {missing}\n'
            'Add an entry to ARCHIVE_FORMATS in shared/archive_formats.py.',
        )

    def test_no_extra_format_entries(self):
        """ARCHIVE_FORMATS should not reference values outside ArchiveType."""
        extra = set(ARCHIVE_FORMATS.keys()) - set(ArchiveType)
        self.assertFalse(extra, f'ARCHIVE_FORMATS keys not in ArchiveType: {extra}')


class TestArchiveFormatFields(unittest.TestCase):
    """Each entry in ARCHIVE_FORMATS must have the required fields."""

    def test_required_fields_present(self):
        for archive_type, info in ARCHIVE_FORMATS.items():
            with self.subTest(archive_type=archive_type.name):
                for field in _REQUIRED_FIELDS:
                    self.assertIn(
                        field, info,
                        f'{archive_type.name} missing required field: {field!r}',
                    )

    def test_category_is_archive_category(self):
        for archive_type, info in ARCHIVE_FORMATS.items():
            with self.subTest(archive_type=archive_type.name):
                self.assertIsInstance(
                    info['category'], ArchiveCategory,
                    f'{archive_type.name} category is not an ArchiveCategory',
                )

    def test_extensions_is_list(self):
        for archive_type, info in ARCHIVE_FORMATS.items():
            with self.subTest(archive_type=archive_type.name):
                self.assertIsInstance(
                    info['extensions'], list,
                    f'{archive_type.name} extensions is not a list',
                )

    def test_pc_formats_have_extensions(self):
        """PC formats (no RISC OS filetype) must have at least one extension."""
        for archive_type, info in ARCHIVE_FORMATS.items():
            if info.get('risc_os_filetype') is None:
                with self.subTest(archive_type=archive_type.name):
                    self.assertTrue(
                        info['extensions'],
                        f'{archive_type.name} has no risc_os_filetype and no extensions — '
                        'it can never be identified',
                    )

    def test_risc_os_filetype_is_lowercase_hex_or_none(self):
        """RISC OS filetypes should be lowercase hex strings or None."""
        import re
        hex_re = re.compile(r'^[0-9a-f]+$')
        for archive_type, info in ARCHIVE_FORMATS.items():
            ft = info.get('risc_os_filetype')
            if ft is not None:
                with self.subTest(archive_type=archive_type.name):
                    self.assertRegex(
                        ft, hex_re,
                        f'{archive_type.name} risc_os_filetype {ft!r} is not lowercase hex',
                    )

    def test_extract_creates_dir_is_bool(self):
        for archive_type, info in ARCHIVE_FORMATS.items():
            with self.subTest(archive_type=archive_type.name):
                self.assertIsInstance(
                    info['extract_creates_dir'], bool,
                    f'{archive_type.name} extract_creates_dir is not a bool',
                )


class TestArchiveFormatHelpers(unittest.TestCase):
    """Helper functions return the right types and values."""

    def test_get_archive_info_known_type(self):
        info = get_archive_info(ArchiveType.ZIP)
        self.assertIsInstance(info, dict)
        self.assertEqual(info['name'], 'ZIP Archive')

    def test_get_archive_info_unknown_type_returns_empty_dict(self):
        # Passing a non-ArchiveType value should return {}
        result = get_archive_info('not_a_type')  # type: ignore[arg-type]
        self.assertEqual(result, {})

    def test_get_archive_by_filetype_known(self):
        self.assertEqual(get_archive_by_filetype('3fb'), ArchiveType.ARCFS)
        self.assertEqual(get_archive_by_filetype('fca'), ArchiveType.SQUASH)

    def test_get_archive_by_filetype_case_insensitive(self):
        self.assertEqual(get_archive_by_filetype('3FB'), ArchiveType.ARCFS)

    def test_get_archive_by_filetype_unknown_returns_none(self):
        self.assertIsNone(get_archive_by_filetype('000'))

    def test_get_archive_by_extension_zip(self):
        self.assertEqual(get_archive_by_extension('archive.zip'), ArchiveType.ZIP)

    def test_get_archive_by_extension_targz(self):
        self.assertEqual(get_archive_by_extension('backup.tar.gz'), ArchiveType.TARGZ)

    def test_get_archive_by_extension_case_insensitive(self):
        self.assertEqual(get_archive_by_extension('ARCHIVE.ZIP'), ArchiveType.ZIP)

    def test_get_archive_by_extension_unknown_returns_none(self):
        self.assertIsNone(get_archive_by_extension('document.pdf'))

    def test_is_archive_format(self):
        self.assertTrue(is_archive_format(ArchiveType.ZIP))
        self.assertTrue(is_archive_format(ArchiveType.TARGZ))
        self.assertFalse(is_archive_format(ArchiveType.GZIP))
        self.assertFalse(is_archive_format(ArchiveType.FCFS))

    def test_is_compressor_format(self):
        self.assertTrue(is_compressor_format(ArchiveType.GZIP))
        self.assertTrue(is_compressor_format(ArchiveType.ZSTD))
        self.assertFalse(is_compressor_format(ArchiveType.ZIP))
        self.assertFalse(is_compressor_format(ArchiveType.FCFS))

    def test_is_disk_image_format(self):
        self.assertTrue(is_disk_image_format(ArchiveType.FCFS))
        self.assertTrue(is_disk_image_format(ArchiveType.DOSDISC))
        self.assertFalse(is_disk_image_format(ArchiveType.ZIP))
        self.assertFalse(is_disk_image_format(ArchiveType.GZIP))

    def test_categories_are_mutually_exclusive(self):
        """Each type should belong to exactly one category."""
        for archive_type in ArchiveType:
            with self.subTest(archive_type=archive_type.name):
                results = [
                    is_archive_format(archive_type),
                    is_compressor_format(archive_type),
                    is_disk_image_format(archive_type),
                ]
                self.assertEqual(
                    sum(results), 1,
                    f'{archive_type.name} matches {sum(results)} category predicates '
                    f'(archive={results[0]}, compress={results[1]}, disk_image={results[2]})',
                )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
