"""
Tests for the Acorn default-filetype hint.

Covers three layers:
  * resolve_default_filetype() / normalize_default_filetype_hint() — the
    ingest-side name/hex resolver and hint normaliser (myapp.riscos_filetypes);
  * enumerate_extracted_files(default_filetype=...) — the worker-side
    application that types otherwise-untyped extracted files.

The worker functions are pure Python (no external tools), so they run in CI.

Run:
    python -m unittest ci.test_default_filetype -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from arcology_shared.hints import HintKey  # noqa: E402
from myapp.riscos_filetypes import (  # noqa: E402
    normalize_default_filetype_hint,
    resolve_default_filetype,
)
from worker.arcworker.tools.extraction import enumerate_extracted_files  # noqa: E402

# =============================================================================
# resolve_default_filetype
# =============================================================================

class TestResolveDefaultFiletype(unittest.TestCase):
    """Name / hex resolution used at ingest."""

    def test_known_name(self):
        self.assertEqual(resolve_default_filetype('Text'), 'fff')

    def test_known_name_case_insensitive(self):
        self.assertEqual(resolve_default_filetype('text'), 'fff')
        self.assertEqual(resolve_default_filetype('DATA'), 'ffd')

    def test_known_hex(self):
        self.assertEqual(resolve_default_filetype('fff'), 'fff')

    def test_hex_uppercased_and_padded(self):
        self.assertEqual(resolve_default_filetype('FFF'), 'fff')
        self.assertEqual(resolve_default_filetype('e'), '00e')

    def test_valid_but_unnamed_hex_accepted(self):
        # RISC OS has 4096 filetypes; the map names only common ones.
        self.assertEqual(resolve_default_filetype('abc'), 'abc')

    def test_unknown_name_rejected(self):
        self.assertIsNone(resolve_default_filetype('definitely-not-a-type'))

    def test_too_long_hex_rejected(self):
        self.assertIsNone(resolve_default_filetype('ffff'))

    def test_empty_returns_none(self):
        self.assertIsNone(resolve_default_filetype(''))


# =============================================================================
# normalize_default_filetype_hint
# =============================================================================

class TestNormalizeHint(unittest.TestCase):
    """The dict-level normaliser used by the REST upload endpoints."""

    def test_name_normalised_to_hex(self):
        hints = {HintKey.ACORN_DEFAULT_FILETYPE: 'Text'}
        out = normalize_default_filetype_hint(hints)
        self.assertEqual(out[HintKey.ACORN_DEFAULT_FILETYPE], 'fff')

    def test_absent_key_left_alone(self):
        hints = {'platform': 'Acorn RISC OS'}
        self.assertEqual(normalize_default_filetype_hint(hints), hints)

    def test_none_hints_ok(self):
        self.assertIsNone(normalize_default_filetype_hint(None))

    def test_blank_value_dropped(self):
        hints = {HintKey.ACORN_DEFAULT_FILETYPE: '   '}
        out = normalize_default_filetype_hint(hints)
        self.assertNotIn(HintKey.ACORN_DEFAULT_FILETYPE, out)

    def test_invalid_value_raises(self):
        hints = {HintKey.ACORN_DEFAULT_FILETYPE: 'nonsense'}
        with self.assertRaises(ValueError):
            normalize_default_filetype_hint(hints)


# =============================================================================
# enumerate_extracted_files(default_filetype=...)
# =============================================================================

class TestEnumerateDefaultFiletype(unittest.TestCase):
    """The worker applies the default only to files no other source typed."""

    def _enumerate(self, layout, *, acorn=False, default_filetype=None):
        """Build a tree from {relpath: bytes} and enumerate it."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        root = Path(tmp)
        for rel, data in layout.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        files = enumerate_extracted_files(
            root, acorn=acorn, default_filetype=default_filetype)
        return {f['path']: f for f in files if not f.get('is_directory')}

    def test_untyped_files_get_default(self):
        by_path = self._enumerate(
            {'readme': b'hello', 'notes': b'world'},
            default_filetype='fff')
        self.assertEqual(by_path['readme']['risc_os_filetype'], 'fff')
        self.assertEqual(by_path['notes']['risc_os_filetype'], 'fff')

    def test_no_default_leaves_untyped(self):
        by_path = self._enumerate({'readme': b'hello'})
        self.assertNotIn('risc_os_filetype', by_path['readme'])

    def test_suffix_typed_files_keep_their_own_type(self):
        # A HostFS/NFS dump mixes ,xxx-suffixed (typed) and plain (untyped)
        # files; the default must not override the explicit suffix type.
        by_path = self._enumerate(
            {'sprite,ff9': b'spr', 'readme': b'hi'},
            acorn=True, default_filetype='fff')
        self.assertEqual(by_path['sprite']['risc_os_filetype'], 'ff9')
        self.assertEqual(by_path['readme']['risc_os_filetype'], 'fff')

    def test_invalid_default_ignored(self):
        by_path = self._enumerate({'readme': b'hi'}, default_filetype='zzzz')
        self.assertNotIn('risc_os_filetype', by_path['readme'])

    def test_default_applies_without_acorn_flag(self):
        # A pure-text dump with no ,xxx files at all still gets defaulted
        # (application is independent of the acorn suffix-parsing flag).
        by_path = self._enumerate(
            {'a': b'1', 'b': b'2'}, acorn=False, default_filetype='fff')
        self.assertEqual(by_path['a']['risc_os_filetype'], 'fff')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
