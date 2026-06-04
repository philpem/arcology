"""
Unit tests for promoting extracted archive contents to derived artefacts.

Focus: a compressed disk image inside a ZIP/sidecar bundle (e.g. the
``drive.dd.zst`` produced by ``arco bulk-import --bundle-sidecars``) must be
recognised as a compressed raw-sector image so the worker promotes it to a
derived artefact and the normal disk-image pipeline runs — rather than leaving
it as an inert ExtractedFile.

Run:
    python -m unittest ci.test_archive_promotion -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from shared.enums import ArtefactType  # noqa: E402
from worker.arcworker.analyses.extraction import (  # noqa: E402
    _promotable_artefact_type,
)


class TestPromotableArtefactType(unittest.TestCase):
    def test_compressed_disk_images_promote_to_compressed_type(self):
        self.assertEqual(_promotable_artefact_type('syq220-01.dd.zst'),
                         ArtefactType.DD_ZST)
        self.assertEqual(_promotable_artefact_type('disk.img.gz'),
                         ArtefactType.DD_GZ)
        self.assertEqual(_promotable_artefact_type('floppy.adf.bz2'),
                         ArtefactType.DD_BZ2)

    def test_uncompressed_images_unchanged(self):
        self.assertEqual(_promotable_artefact_type('drive.dd'),
                         ArtefactType.RAW_SECTOR)
        self.assertEqual(_promotable_artefact_type('image.iso'),
                         ArtefactType.ISO)
        self.assertEqual(_promotable_artefact_type('flux.scp'),
                         ArtefactType.SCP)

    def test_case_insensitive(self):
        self.assertEqual(_promotable_artefact_type('DRIVE.DD.ZST'),
                         ArtefactType.DD_ZST)

    def test_compressed_non_images_not_promoted(self):
        # A compressor suffix on something that is not a raw-sector image must
        # not be mistaken for a disk image.
        for name in ('data.tar.gz', 'notes.txt.gz', 'archive.gz',
                     'manifest.json.zst', 'readme.bz2'):
            self.assertIsNone(_promotable_artefact_type(name), name)

    def test_unknown_extensions_not_promoted(self):
        for name in ('readme.txt', 'drive.map', 'foo', 'photo.jpg'):
            self.assertIsNone(_promotable_artefact_type(name), name)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
