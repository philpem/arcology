"""
Regression test for View / Module-Information buttons on files inside nested
archives (e.g. ArcFS archives on an ADFS disc).

Bug: the follow-on analyses (FORMAT_CONVERT, RISCOS_MODULE_PARSE, REPLAY_PROCESS,
MEDIA_TRANSCODE) each iterate an extraction's files via ``iter_resolved_files``
and record the third yielded value as the file's identity — FORMAT_CONVERT's
``source_file``, ``RiscosModule.file_path``, ``ReplayMovie.file_path`` and
``MediaFile.file_path``.  The web file listing lights up the View / info buttons
by matching those recorded values against ``ExtractedFile.path``.

``iter_resolved_files`` used to yield the *path_prefix-stripped* disk path (the
value ``resolve_extraction_file`` returns for locating the bytes on disk).  For a
file inside a nested archive the queued job carries ``path_prefix`` = the
archive's display path, so the stripped path is the bare in-archive name
(``ArcFSFiler``) while the DB stores the full path
(``!Inst_Kill/Killer/!ARCFS/RESOURCES/ArcFSFiler``).  The recorded values then
never matched ``ExtractedFile.path`` and the buttons silently vanished for every
file inside an archive — while identical files extracted straight from the disc
(no ``path_prefix``, nothing stripped) kept theirs.

This test pins ``iter_resolved_files`` to yield the full DB path regardless of
``path_prefix``, while still resolving the bytes via the stripped disk path.

Run:
    python -m unittest ci.test_nested_archive_file_paths -v
"""

import os
import sys
import unittest
from pathlib import Path
from typing import ClassVar

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.analyses._common import iter_resolved_files


class _FakeWorker:
    """Minimal stand-in exposing only what resolve_extraction_file needs.

    ``_resolve_single_extraction_file`` is the single-file locator the real
    worker implements; here it "finds" a file iff the requested disk-relative
    path is in ``present`` and returns a fake on-disk Path.  Every requested
    disk path is recorded so the test can assert the bytes are still located via
    the prefix-stripped path.
    """

    def __init__(self, present):
        self.present = set(present)
        self.requested = []

    def _resolve_single_extraction_file(self, extraction_path, relative_path,
                                        dest_dir, risc_os_filetype=None):
        self.requested.append(relative_path)
        if relative_path in self.present:
            return Path('/on-disk') / relative_path
        return None


class NestedArchiveFilePathTest(unittest.TestCase):
    PREFIX = '!Inst_Kill/Killer/!ARCFS/RESOURCES'
    FILES: ClassVar[list] = [
        {'path': f'{PREFIX}/ArcFSFiler', 'risc_os_filetype': 'ffa'},
        {'path': f'{PREFIX}/ImageFSFix', 'risc_os_filetype': 'ffa'},
        {'path': f'{PREFIX}/ResourceFS', 'risc_os_filetype': 'ffa'},
    ]

    def test_yields_full_db_path_not_stripped_disk_path(self):
        # On disk the archive extractor laid the files down under their bare,
        # prefix-stripped names — that is what the worker must read.
        worker = _FakeWorker(present={'ArcFSFiler', 'ImageFSFix', 'ResourceFS'})

        yielded = list(iter_resolved_files(
            worker, self.FILES, extraction_path='out/resources_extract',
            work_dir=Path('/tmp/work'), path_prefix=self.PREFIX,
        ))

        # The recorded identity (third value) must be the full ExtractedFile.path
        # so the web listing can match it — NOT the stripped in-archive name.
        recorded = [db_path for _fd, _fp, db_path in yielded]
        self.assertEqual(recorded, [f['path'] for f in self.FILES])
        for db_path in recorded:
            self.assertTrue(db_path.startswith(self.PREFIX + '/'), db_path)

        # The bytes are still located via the prefix-stripped disk path.
        self.assertEqual(
            worker.requested, ['ArcFSFiler', 'ImageFSFix', 'ResourceFS'])

    def test_no_prefix_is_unaffected(self):
        # Files extracted straight from the disc carry no path_prefix; the DB
        # path and disk path coincide and both behaviours must be identical.
        files = [{'path': '!Inst_Kill/Killer/!ARCFS/ARCFS', 'risc_os_filetype': 'ffa'}]
        worker = _FakeWorker(present={'!Inst_Kill/Killer/!ARCFS/ARCFS'})

        yielded = list(iter_resolved_files(
            worker, files, extraction_path='out/file_extraction',
            work_dir=Path('/tmp/work'), path_prefix='',
        ))

        self.assertEqual([db for _fd, _fp, db in yielded],
                         ['!Inst_Kill/Killer/!ARCFS/ARCFS'])

    def test_unresolvable_file_invokes_on_missing_with_full_path(self):
        worker = _FakeWorker(present=set())  # nothing on disk
        missing = []

        yielded = list(iter_resolved_files(
            worker, self.FILES, extraction_path='out/resources_extract',
            work_dir=Path('/tmp/work'), path_prefix=self.PREFIX,
            on_missing=lambda fd, db_path: missing.append(db_path),
        ))

        self.assertEqual(yielded, [])
        self.assertEqual(missing, [f['path'] for f in self.FILES])


if __name__ == '__main__':
    unittest.main()
