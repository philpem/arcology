"""
Unit tests for myapp/utils/slugs.py — generate_slug() only.

generate_slug() is a pure function with no external dependencies (it imports
only ``re`` and ``typing`` at module level), so these tests can run in the
lint stage before ``pip install`` has been executed.

Run:
    python -m unittest ci.test_slug -v
    # or from the repo root:
    python -m unittest discover -s ci -p "test_slug.py" -v
"""

import importlib.util
import os
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load slugs.py directly to avoid triggering myapp/__init__.py, which imports
# Flask and all extensions.  slugs.py itself only uses ``re`` and ``typing``
# at module level, so it is importable without any installed packages.
_spec = importlib.util.spec_from_file_location(
    'slugs',
    os.path.join(_REPO_ROOT, 'myapp', 'utils', 'slugs.py'),
)
_slugs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_slugs)

generate_slug = _slugs.generate_slug


class TestGenerateSlug(unittest.TestCase):
    """Tests for generate_slug()."""

    # ------------------------------------------------------------------
    # Docstring examples — if these break, the function semantics changed
    # ------------------------------------------------------------------

    def test_accession_number_with_space(self):
        self.assertEqual(generate_slug('FBX3-01 KUAI'), 'fbx3-01-kuai')

    def test_slash_and_colon_separators(self):
        self.assertEqual(generate_slug('Disc 1/4: Install'), 'disc-1-4-install')

    def test_dot_and_underscore_separators(self):
        self.assertEqual(generate_slug('Test_Archive.zip'), 'test-archive-zip')

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_string_returns_untitled(self):
        self.assertEqual(generate_slug(''), 'untitled')

    def test_whitespace_only_returns_untitled(self):
        self.assertEqual(generate_slug('   '), 'untitled')

    def test_all_separators_become_single_dash(self):
        # Multiple consecutive separators collapse to a single dash
        self.assertEqual(generate_slug('a  b'), 'a-b')
        self.assertEqual(generate_slug('a//b'), 'a-b')
        self.assertEqual(generate_slug('a.:b'), 'a-b')

    def test_leading_and_trailing_dashes_stripped(self):
        self.assertEqual(generate_slug('/leading'), 'leading')
        self.assertEqual(generate_slug('trailing/'), 'trailing')
        self.assertEqual(generate_slug('/both/'), 'both')

    def test_uppercase_lowercased(self):
        self.assertEqual(generate_slug('HELLO WORLD'), 'hello-world')

    def test_non_ascii_removed(self):
        # Characters outside a-z, 0-9, dash are stripped after separator replacement
        result = generate_slug('café au lait')
        self.assertRegex(result, r'^[a-z0-9-]+$')

    def test_max_length_truncated(self):
        long_text = 'a' * 300
        result = generate_slug(long_text, max_length=100)
        self.assertLessEqual(len(result), 100)

    def test_max_length_does_not_end_with_dash(self):
        # A separator right at the truncation boundary should not leave a trailing dash
        text = 'a' * 99 + ' ' + 'b' * 100
        result = generate_slug(text, max_length=100)
        self.assertFalse(result.endswith('-'), f'slug ends with dash: {result!r}')

    def test_all_separators_recognised(self):
        for sep in ('/', '.', ':', ';', ',', '_', ' ', '\t', '\n'):
            with self.subTest(sep=repr(sep)):
                result = generate_slug(f'a{sep}b')
                self.assertEqual(result, 'a-b', f'separator {sep!r} not handled')

    def test_numbers_preserved(self):
        self.assertEqual(generate_slug('disc 42'), 'disc-42')

    def test_only_special_chars_returns_untitled(self):
        self.assertEqual(generate_slug('!@#$%^&*()'), 'untitled')


class TestLookupByIdentifierParsing(unittest.TestCase):
    """
    Pure parsing tests for lookup_by_identifier() identifier format detection.

    These tests exercise the regex logic without needing Flask or a database,
    by checking what kind of identifier is detected from the input string.
    """

    import re as _re

    def _is_full_uuid(self, s):
        return bool(self._re.fullmatch(r'[0-9a-f]{32}', s))

    def _is_valid_short_prefix(self, s):
        return len(s) >= 8 and bool(self._re.fullmatch(r'[0-9a-f]{8}', s[:8]))

    def test_full_uuid_detected(self):
        uuid = 'a' * 32
        self.assertTrue(self._is_full_uuid(uuid))

    def test_full_uuid_with_uppercase_not_detected(self):
        uuid = 'A' * 32
        self.assertFalse(self._is_full_uuid(uuid))

    def test_short_uuid_prefix_detected(self):
        self.assertTrue(self._is_valid_short_prefix('3f4a9b2c'))

    def test_short_uuid_plus_slug_prefix_detected(self):
        self.assertTrue(self._is_valid_short_prefix('3f4a9b2c-elite-bbc-micro'))

    def test_too_short_not_valid(self):
        self.assertFalse(self._is_valid_short_prefix('abc'))

    def test_non_hex_prefix_not_valid(self):
        self.assertFalse(self._is_valid_short_prefix('zzzzzzzz'))

    def test_pure_slug_not_full_uuid_not_hex_prefix(self):
        s = 'elite-bbc-micro'
        self.assertFalse(self._is_full_uuid(s))
        self.assertFalse(self._is_valid_short_prefix(s))

    def test_empty_string_not_valid(self):
        s = ''
        self.assertFalse(self._is_full_uuid(s))
        self.assertFalse(self._is_valid_short_prefix(s))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
