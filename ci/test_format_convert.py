"""
Regression tests for the FORMAT_CONVERT extraction-scan handler
(worker/arcworker/analyses/images.py, process_format_convert Mode 2).

Focus: the viewable-type map is keyed by the *DB* path (ExtractedFile.path,
which carries the archive's display-path prefix), but iter_resolved_files
yields the prefix-*stripped* on-disk path as its third value.  Keying the
lookup by that stripped path raised KeyError for archive-extracted files
(those with a path_prefix).  See the worker logs in the issue:
``KeyError: '!HDBackup/!Boot'`` / ``KeyError: '2F8B1A7A'``.

No real worker / API — a stub stands in for ``self``.

Run:
    python -m unittest ci.test_format_convert -v
"""

import json
import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from arcology_shared.enums import ArtefactType
from arcology_shared.hints import HintKey
from worker.arcworker.analyses.images import process_format_convert


class _StubAPI:
    def __init__(self, files):
        self._files = files

    def get_partition_files(self, partition_uuid, **params):
        return list(self._files)

    def update_analysis(self, *a, **k):  # only called on failure paths
        raise AssertionError(f'update_analysis should not be called: {a} {k}')


class _StubWorker:
    """Minimal stand-in for AnalysisWorker for process_format_convert Mode 2."""

    def __init__(self, files, resolvable):
        self.api = _StubAPI(files)
        self._resolvable = resolvable
        self.completed = None
        self.failed = None

    # --- scaffold hooks ---
    def _resolve_single_extraction_file(self, extraction_path, disk_path, work_dir,
                                        risc_os_filetype=None):
        got = self._resolvable.get(disk_path)
        return Path(got) if got else None

    # --- conversion: pretend every file converts to a single image output ---
    def _convert_file_to_outputs(self, file_path, artefact_type, work_dir,
                                 output_subdir, analysis_uuid, file_index=0):
        return ([{'type': 'image', 'filename': f'out{file_index}.png'}], None, [])

    # --- terminal reporting ---
    def complete_analysis(self, analysis_id, *, summary='', details=''):
        self.completed = {'id': analysis_id, 'summary': summary,
                          'details': json.loads(details) if details else {}}

    def fail_analysis(self, analysis_id, message):
        self.failed = {'id': analysis_id, 'message': message}


def _file(path, *, filename=None, ft=None):
    return {
        'path': path,
        'filename': filename if filename is not None else path.split('/')[-1],
        'is_directory': False,
        'risc_os_filetype': ft,
    }


def _analysis(hints):
    return {'id': 99, 'uuid': 'conv-uuid', 'hints': json.dumps(hints)}


def _artefact():
    # Mode 2: artefact_type is not one of the direct convertible types, so the
    # handler takes the extraction-scan path.
    return {'uuid': 'art-uuid', 'artefact_type': ArtefactType.ISO.value,
            'item': {'uuid': 'item-uuid', 'slug': 'item'}}


class TestFormatConvertExtractionScan(unittest.TestCase):

    def test_path_prefix_does_not_raise_keyerror(self):
        """Archive-extracted viewable file (with a path_prefix) converts cleanly.

        The DB path keeps the archive prefix; the on-disk path strips it.  The
        viewable-type lookup must use the DB path, not the stripped disk path.
        """
        # A RISC OS sprite (filetype FF9) inside an archive: DB path carries the
        # archive's display-path prefix; the file sits prefix-stripped on disk.
        db_path = '!HDBackup/sprite,ff9'
        disk_path = 'sprite,ff9'  # prefix '!HDBackup' stripped
        files = [_file(db_path, filename='sprite,ff9', ft='ff9')]
        w = _StubWorker(files, resolvable={disk_path: '/disk/sprite,ff9'})
        hints = {
            HintKey.PARTITION_UUID: 'p',
            HintKey.EXTRACTION_PATH: 'ext',
            HintKey.PATH_PREFIX: '!HDBackup',
        }

        process_format_convert(w, _analysis(hints), _artefact(), Path('/work'))

        # Must have completed (not failed, not raised KeyError).
        self.assertIsNone(w.failed)
        self.assertIsNotNone(w.completed)
        details = w.completed['details']
        self.assertEqual(details['mode'], 'extraction_scan')
        self.assertEqual(len(details['outputs']), 1)
        # source_file is the DB path (matches ExtractedFile.path), not the
        # prefix-stripped on-disk path.
        self.assertEqual(details['outputs'][0]['source_file'], db_path)
        self.assertEqual(details['failed_conversions'], [])

    def test_no_prefix_still_works(self):
        """Disc-level extraction (no path_prefix): DB path == on-disk path."""
        db_path = 'sprite,ff9'
        files = [_file(db_path, filename='sprite,ff9', ft='ff9')]
        w = _StubWorker(files, resolvable={db_path: '/disk/sprite,ff9'})
        hints = {HintKey.PARTITION_UUID: 'p', HintKey.EXTRACTION_PATH: 'ext'}

        process_format_convert(w, _analysis(hints), _artefact(), Path('/work'))

        self.assertIsNone(w.failed)
        self.assertIsNotNone(w.completed)
        self.assertEqual(
            w.completed['details']['outputs'][0]['source_file'], db_path)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
