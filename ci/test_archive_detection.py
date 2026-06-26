"""Tests for inline archive detection (detect_and_queue_archives).

Archive detection used to be a standalone ARCHIVE_DETECT analysis; it is now
folded into file registration.  detect_and_queue_archives() walks the just-
registered files, marks each archive via /files/{id}/mark_archive, and queues one
ARCHIVE_EXTRACT per archive using the DB ids returned by registration — skipping
compressed disk images and respecting the recursion depth cap.

Run:
    python -m unittest ci.test_archive_detection -v
"""

import os
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from arcology_shared.enums import AnalysisType
from arcology_shared.hints import HintKey
from worker.arcworker.analyses.extraction import detect_and_queue_archives
from worker.arcworker.config import MAX_ARCHIVE_DEPTH


def _run(files, path_to_id, **kwargs):
    """Drive detect_and_queue_archives with a stub API; return (marks, queued)."""
    marks = []     # (file_id, body) from POST /files/{id}/mark_archive
    queued = []    # (analysis_type, hints) from queue_analysis

    def _post(path, body):
        # path is "/files/<id>/mark_archive"
        marks.append((int(path.split('/')[2]), body))

    api = types.SimpleNamespace(
        post=_post,
        queue_analysis=lambda uuid, atype, hints=None: queued.append((atype, hints)),
    )
    fake = types.SimpleNamespace(api=api)
    detect_and_queue_archives(fake, 'art-uuid', files, path_to_id, 'part-uuid', **kwargs)
    return marks, queued


class TestDetectAndQueueArchives(unittest.TestCase):
    def test_zip_marked_and_extraction_queued(self):
        files = [{'path': 'games/disk.zip', 'extraction_depth': 0}]
        marks, queued = _run(files, {'games/disk.zip': 42},
                             extraction_path='out')
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0][0], 42)
        self.assertTrue(marks[0][1]['is_archive'])
        self.assertEqual(len(queued), 1)
        atype, hints = queued[0]
        self.assertEqual(atype, AnalysisType.ARCHIVE_EXTRACT.value)
        self.assertEqual(hints[HintKey.FILE_ID], 42)
        self.assertEqual(hints[HintKey.EXTRACTION_DEPTH], 1)   # depth + 1
        self.assertEqual(hints[HintKey.EXTRACTION_PATH], 'out')

    def test_non_archive_ignored(self):
        marks, queued = _run([{'path': 'notes/readme.txt'}], {'notes/readme.txt': 1})
        self.assertEqual(marks, [])
        self.assertEqual(queued, [])

    def test_directory_skipped(self):
        marks, queued = _run(
            [{'path': 'games', 'is_directory': True}], {'games': 9})
        self.assertEqual((marks, queued), ([], []))

    def test_compressed_disk_image_skipped(self):
        # drive.dd.zst is promoted to a disk-image artefact elsewhere; it must
        # not be treated as a generic .zst compressor here.
        marks, queued = _run(
            [{'path': 'drive.dd.zst', 'extraction_depth': 0}], {'drive.dd.zst': 7})
        self.assertEqual((marks, queued), ([], []))

    def test_depth_cap_marks_but_does_not_recurse(self):
        files = [{'path': 'deep.zip', 'extraction_depth': MAX_ARCHIVE_DEPTH}]
        marks, queued = _run(files, {'deep.zip': 5}, extraction_path='out')
        self.assertEqual(len(marks), 1)        # still marked as an archive
        self.assertEqual(queued, [])           # but no further extraction

    def test_missing_id_skips_quietly(self):
        # No registered id for the path → no extraction queued (no crash).
        marks, queued = _run([{'path': 'a.zip', 'extraction_depth': 0}], {})
        self.assertEqual((marks, queued), ([], []))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
