"""
Regression test for re-running an extraction into a deterministic output dir.

FILE_EXTRACTION and top-level ARCHIVE_EXTRACT write into a per-analysis output
directory whose path is derived solely from the analysis UUID
(`get_output_path()`), so a re-run — stale-job reset, worker restart mid-job —
targets the *same* directory.  The first run's `normalize_extracted_filenames()`
renames RISC OS Latin-1 byte names to their Unicode forms (e.g. raw byte 0xE7 →
'ç'); if the directory is not cleared first, the second run's freshly extracted
raw-byte names land alongside the already-normalised Unicode names.  Normalise
then sees the Unicode target already present, logs "target exists", and skips —
leaving a surrogate-escaped duplicate and a corrupted listing.

These tests exercise the real `normalize_extracted_filenames()` against a fake
"extractor" that writes raw RISC OS byte names, plus the real
`reset_output_dir()` helper the handlers call before extracting, and verify
that clearing the output dir before each run keeps a re-run idempotent.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# Raw RISC OS Latin-1 byte names DIM would write to disk.  0xE7 = 'ç', 0xA0 =
# non-breaking space — both single bytes, invalid as standalone UTF-8.
_RAW_NAMES = {
    b'Fran\xe7ais': b'language data',
    b'To\xa0do': b'todo list',
}


def _fake_extract(output_dir: Path) -> None:
    """Simulate an extraction tool writing raw RISC OS byte filenames.

    Mirrors what DiscImageManager does: `mkdir(exist_ok=True)` then write files,
    without clearing pre-existing content.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for raw_name, data in _RAW_NAMES.items():
        with open(os.path.join(os.fsencode(str(output_dir)), raw_name), 'wb') as fh:
            fh.write(data)


def _listing(output_dir: Path) -> list[bytes]:
    return sorted(os.listdir(os.fsencode(str(output_dir))))


class TestExtractionRerun(unittest.TestCase):

    def test_single_run_normalises_to_unicode(self):
        """A clean first run leaves only the Unicode names on disk."""
        from worker.arcworker.utils.text import normalize_extracted_filenames

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'abc_file_extraction'
            _fake_extract(out)
            normalize_extracted_filenames(out)

            names = _listing(out)
            self.assertEqual(
                names,
                sorted(['Français'.encode(), 'To\xa0do'.encode()]),
            )

    def test_rerun_without_clearing_corrupts(self):
        """Documents the bug: re-extracting without clearing strands raw names.

        After the first run normalises 'Fran\\xe7ais' → 'Français', a second
        extraction re-creates the raw-byte name; normalise cannot rename it
        (target exists), so both coexist and a surrogate-named duplicate is left
        behind.
        """
        from worker.arcworker.utils.text import normalize_extracted_filenames

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'abc_file_extraction'

            _fake_extract(out)
            normalize_extracted_filenames(out)
            _fake_extract(out)              # re-run WITHOUT clearing
            normalize_extracted_filenames(out)

            names = _listing(out)
            # The raw byte names survive alongside the Unicode ones — corruption.
            self.assertIn(b'Fran\xe7ais', names)
            self.assertIn('Français'.encode(), names)
            self.assertEqual(len(names), 2 * len(_RAW_NAMES))

    def test_reset_output_dir_clears_existing_tree(self):
        """The production helper removes a prior run's content, returns the path.

        Guards the real code the handlers call: if reset_output_dir() stopped
        clearing, this fails.
        """
        from worker.arcworker.utils.paths import reset_output_dir

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'abc_file_extraction'
            _fake_extract(out)
            (out / 'sub').mkdir()
            (out / 'sub' / 'nested').write_bytes(b'stale')

            returned = reset_output_dir(out)

            self.assertEqual(returned, out)        # returns the path it cleared
            self.assertFalse(out.exists())         # tree fully removed

        # Missing path is a no-op, not an error.
        reset_output_dir(Path(tmp) / 'gone')

    def test_rerun_with_clearing_is_idempotent(self):
        """The fix: clearing the output dir before each run keeps it clean.

        Drives the real `reset_output_dir()` helper the FILE_EXTRACTION /
        ARCHIVE_EXTRACT handlers call before invoking the extractor — so a
        regression that stops it clearing would fail here too.
        """
        from worker.arcworker.utils.paths import reset_output_dir
        from worker.arcworker.utils.text import normalize_extracted_filenames

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'abc_file_extraction'

            def run_once():
                reset_output_dir(out)            # production clear-before-extract
                _fake_extract(out)
                normalize_extracted_filenames(out)

            run_once()
            first = _listing(out)
            run_once()
            second = _listing(out)

            expected = sorted(['Français'.encode(), 'To\xa0do'.encode()])
            self.assertEqual(first, expected)
            self.assertEqual(second, expected)   # idempotent across re-runs


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
