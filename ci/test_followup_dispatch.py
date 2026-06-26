"""Tests for content-gated follow-up dispatch (queue_partition_follow_ups).

After an extraction, a per-kind follow-on analysis (FORMAT_CONVERT, REPLAY_PROCESS,
MEDIA_TRANSCODE, RISCOS_MODULE_PARSE, ARCHIVE_DETECT) is queued only when the
extraction actually contains that kind of content; PRODUCT_RECOGNITION is
hash-based and always runs.  These tests drive AnalysisWorker.queue_partition_follow_ups
with a stub API and assert exactly which analyses are queued for a given
present-categories set.

Run:
    python -m unittest ci.test_followup_dispatch -v
"""

import os
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from arcology_shared.content_categories import ContentCategory as C
from arcology_shared.enums import AnalysisType
from worker.arcworker.analysis import AnalysisWorker


def _queued(categories, *, extraction_path='out'):
    """Return the set of AnalysisType.value strings queued for *categories*."""
    queued = []
    api = types.SimpleNamespace(
        queue_analysis=lambda uuid, atype, hints=None: queued.append(atype))
    fake = types.SimpleNamespace(api=api)
    AnalysisWorker.queue_partition_follow_ups(
        fake, 'art-uuid', 'part-uuid',
        extraction_path=extraction_path, categories=categories)
    return set(queued)


class TestFollowUpGating(unittest.TestCase):
    def test_none_queues_everything(self):
        # Pre-gating behaviour: unknown categories → queue all follow-ups.
        self.assertEqual(_queued(None), {
            AnalysisType.ARCHIVE_DETECT.value,
            AnalysisType.PRODUCT_RECOGNITION.value,
            AnalysisType.FORMAT_CONVERT.value,
            AnalysisType.RISCOS_MODULE_PARSE.value,
            AnalysisType.REPLAY_PROCESS.value,
            AnalysisType.MEDIA_TRANSCODE.value,
        })

    def test_empty_queues_only_product_recognition(self):
        self.assertEqual(
            _queued(set()), {AnalysisType.PRODUCT_RECOGNITION.value})

    def test_replay_only(self):
        self.assertEqual(_queued({C.REPLAY}), {
            AnalysisType.PRODUCT_RECOGNITION.value,
            AnalysisType.REPLAY_PROCESS.value,
        })

    def test_media_only(self):
        self.assertEqual(_queued({C.MEDIA}), {
            AnalysisType.PRODUCT_RECOGNITION.value,
            AnalysisType.MEDIA_TRANSCODE.value,
        })

    def test_archive_and_convertible(self):
        self.assertEqual(_queued({C.ARCHIVE, C.CONVERTIBLE}), {
            AnalysisType.PRODUCT_RECOGNITION.value,
            AnalysisType.ARCHIVE_DETECT.value,
            AnalysisType.FORMAT_CONVERT.value,
        })

    def test_format_convert_needs_extraction_path(self):
        # FORMAT_CONVERT is extraction-scoped: no path → not queued even when
        # convertible content is present.
        self.assertEqual(
            _queued({C.CONVERTIBLE}, extraction_path=None),
            {AnalysisType.PRODUCT_RECOGNITION.value})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
