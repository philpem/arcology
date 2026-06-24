"""
Unit tests for the reusable per-partition batch scaffold in
worker/arcworker/analyses/_common.py.

Covers scan_partition_files (hint parse, select filter, directory exclusion,
lazy extraction-path fallback) and iter_resolved_files (resolve, on_missing,
progress reporting).  No real worker / API — a stub stands in for ``self``.

Run:
    python -m unittest ci.test_batch_scaffold -v
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

from arcology_shared.hints import HintKey
from worker.arcworker.analyses._common import (
    iter_resolved_files,
    scan_partition_files,
)


class _StubAPI:
    def __init__(self, files):
        self._files = files
        self.calls = []

    def get_partition_files(self, partition_uuid, **params):
        self.calls.append((partition_uuid, params))
        return list(self._files)


class _StubWorker:
    """Minimal stand-in for AnalysisWorker for the scaffold helpers."""

    def __init__(self, files, resolvable=None, fallback_path=None):
        self.api = _StubAPI(files)
        # Map of db_path -> resolved Path (None / missing → not found).
        self._resolvable = resolvable or {}
        self._fallback_path = fallback_path
        self.fallback_calls = 0

    def _resolve_single_extraction_file(self, extraction_path, disk_path, work_dir,
                                        risc_os_filetype=None):
        got = self._resolvable.get(disk_path)
        return Path(got) if got else None


def _file(path, *, is_dir=False, filename=None, ft=None):
    return {
        'path': path,
        'filename': filename if filename is not None else path.split('/')[-1],
        'is_directory': is_dir,
        'risc_os_filetype': ft,
    }


def _analysis(hints=None):
    return {'id': 1, 'uuid': 'a-uuid', 'hints': json.dumps(hints) if hints else None}


def _artefact():
    return {'uuid': 'art-uuid'}


class TestScanPartitionFiles(unittest.TestCase):

    def test_missing_partition_uuid_returns_none(self):
        w = _StubWorker([])
        self.assertIsNone(
            scan_partition_files(w, _analysis({}), _artefact(), select_files=lambda f: True))

    def test_filters_and_excludes_directories(self):
        files = [
            _file('a.avi'),
            _file('d', is_dir=True),
            _file('b.txt'),
            _file('c.mov'),
        ]
        w = _StubWorker(files)
        hints = {HintKey.PARTITION_UUID: 'p', HintKey.EXTRACTION_PATH: 'ext'}
        scan = scan_partition_files(
            w, _analysis(hints), _artefact(),
            select_files=lambda f: f['filename'].endswith(('.avi', '.mov')))
        self.assertIsNotNone(scan)
        self.assertEqual({f['path'] for f in scan.files}, {'a.avi', 'c.mov'})
        self.assertEqual(scan.extraction_path, 'ext')
        # Directory was excluded before select_files saw it.
        self.assertNotIn('d', {f['path'] for f in scan.files})

    def test_path_prefix_passed_to_api(self):
        w = _StubWorker([_file('x.mp4')])
        hints = {HintKey.PARTITION_UUID: 'p', HintKey.EXTRACTION_PATH: 'ext',
                 HintKey.PATH_PREFIX: 'Archives/foo.zip'}
        scan = scan_partition_files(w, _analysis(hints), _artefact(),
                                    select_files=lambda f: True)
        self.assertEqual(scan.path_prefix, 'Archives/foo.zip')
        _, params = w.api.calls[0]
        self.assertEqual(params.get('path_prefix'), 'Archives/foo.zip')
        self.assertNotIn('extraction_depth', params)

    def test_extraction_depth_when_no_prefix(self):
        w = _StubWorker([_file('x.mp4')])
        hints = {HintKey.PARTITION_UUID: 'p', HintKey.EXTRACTION_PATH: 'ext'}
        scan_partition_files(w, _analysis(hints), _artefact(), select_files=lambda f: True)
        _, params = w.api.calls[0]
        self.assertEqual(params.get('extraction_depth'), 0)

    def test_lazy_fallback_not_called_when_no_matches(self):
        """find_extraction_path is skipped when nothing matched (no path needed)."""
        called = {'n': 0}

        import worker.arcworker.analyses._common as common
        orig = common.find_extraction_path

        def _spy(self, uuid):
            called['n'] += 1
            return 'fallback/ext'
        common.find_extraction_path = _spy
        try:
            w = _StubWorker([_file('a.txt')])  # nothing matches the media filter
            hints = {HintKey.PARTITION_UUID: 'p'}  # no extraction_path hint
            scan = scan_partition_files(
                w, _analysis(hints), _artefact(),
                select_files=lambda f: f['filename'].endswith('.mp4'))
            self.assertEqual(scan.files, [])
            self.assertEqual(called['n'], 0, 'fallback must not run when no files matched')
        finally:
            common.find_extraction_path = orig

    def test_lazy_fallback_called_when_matches_and_no_hint(self):
        import worker.arcworker.analyses._common as common
        orig = common.find_extraction_path
        common.find_extraction_path = lambda self, uuid: 'fallback/ext'
        try:
            w = _StubWorker([_file('a.mp4')])
            hints = {HintKey.PARTITION_UUID: 'p'}  # no extraction_path hint
            scan = scan_partition_files(
                w, _analysis(hints), _artefact(),
                select_files=lambda f: f['filename'].endswith('.mp4'))
            self.assertEqual(scan.extraction_path, 'fallback/ext')
        finally:
            common.find_extraction_path = orig

    def test_unresolvable_path_returns_none(self):
        import worker.arcworker.analyses._common as common
        orig = common.find_extraction_path
        common.find_extraction_path = lambda self, uuid: None
        try:
            w = _StubWorker([_file('a.mp4')])
            hints = {HintKey.PARTITION_UUID: 'p'}
            scan = scan_partition_files(
                w, _analysis(hints), _artefact(),
                select_files=lambda f: True)
            self.assertIsNone(scan)
        finally:
            common.find_extraction_path = orig


class TestIterResolvedFiles(unittest.TestCase):

    def test_yields_resolved_and_skips_missing(self):
        files = [_file('a.mp4'), _file('b.mp4'), _file('c.mp4')]
        w = _StubWorker(files, resolvable={'a.mp4': '/disk/a.mp4', 'c.mp4': '/disk/c.mp4'})
        missing = []
        out = list(iter_resolved_files(
            w, files, 'ext', Path('/work'),
            on_missing=lambda fd, p: missing.append(p)))
        self.assertEqual([fd['path'] for fd, _fp, _dp in out], ['a.mp4', 'c.mp4'])
        self.assertEqual(missing, ['b.mp4'])

    def test_progress_reporter_driven(self):
        files = [_file('a.mp4'), _file('b.mp4')]
        w = _StubWorker(files, resolvable={'a.mp4': '/disk/a.mp4', 'b.mp4': '/disk/b.mp4'})

        seen = []

        class _Reporter:
            def update(self, n):
                seen.append(n)
                return True

        list(iter_resolved_files(w, files, 'ext', Path('/work'), reporter=_Reporter()))
        self.assertEqual(seen, [1, 2])

    def test_path_prefix_stripped(self):
        # db path carries the archive prefix; disk path strips it.
        files = [_file('Archives/foo.zip/clip.mp4')]
        w = _StubWorker(files, resolvable={'clip.mp4': '/disk/clip.mp4'})
        out = list(iter_resolved_files(
            w, files, 'ext', Path('/work'), path_prefix='Archives/foo.zip'))
        self.assertEqual(len(out), 1)
        self.assertEqual(str(out[0][1]), '/disk/clip.mp4')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
