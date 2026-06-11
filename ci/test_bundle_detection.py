"""
Tests for worker-side disk-image-bundle detection: a ZIP of exactly one
compressed disk image plus sidecars is recognised (by marker or content sniff),
while generic archives and RISC-OS-ish zips are not.
"""

import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


import worker.arcworker.analyses.extraction as ext  # noqa: E402
from shared.bundle import BUNDLE_MARKER  # noqa: E402
from worker.arcworker.analyses.extraction import (  # noqa: E402
    _disk_image_bundle_member,
    _image_base_name,
)


def _make_zip(members, comment=None):
    d = tempfile.mkdtemp()
    path = Path(d) / 'bundle.zip'
    with zipfile.ZipFile(path, 'w') as zf:
        if comment is not None:
            zf.comment = comment
        for name in members:
            zf.writestr(name, b'x' * 100)
    return path


class TestImageBaseName(unittest.TestCase):
    def test_strips_compound_extension(self):
        self.assertEqual(_image_base_name('syq220-01.dd.zst'), 'syq220-01')
        self.assertEqual(_image_base_name('disk.img.gz'), 'disk')
        self.assertEqual(_image_base_name('drive.dd'), 'drive')


class TestBundleDetection(unittest.TestCase):
    def test_marker_bundle(self):
        z = _make_zip(['syq220-01.dd.zst', 'syq220-01.map', 'README.txt'],
                      comment=BUNDLE_MARKER.encode('cp437'))
        self.assertEqual(_disk_image_bundle_member(z), 'syq220-01.dd.zst')

    def test_unmarked_bundle_shape_is_not_transformed(self):
        # Even a perfectly bundle-shaped zip is left alone without the marker —
        # the transform is destructive (drops the zip), so it is marker-only.
        z = _make_zip(['drive.dd.zst', 'drive.map', 'drive.md5'])
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_generic_archive_rejected(self):
        z = _make_zip(['a.txt', 'b.doc', 'c.pdf'])
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_two_images_rejected(self):
        z = _make_zip(['a.dd.zst', 'b.dd.zst', 'a.map'])
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_image_plus_non_sidecar_rejected(self):
        # An unrelated non-sidecar member means it isn't a clean bundle.
        z = _make_zip(['drive.dd.zst', 'program.exe'])
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_riscos_archive_with_coincidental_image_rejected(self):
        # Many files + one .dd.zst, no marker -> not a bundle.
        members = ['!Boot', '!Run', 'Sprites', 'data.dd.zst', 'Docs/manual']
        z = _make_zip(members)
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_marker_with_extra_non_sidecar_member_rejected(self):
        # The marker is required, but the structural guard still applies: a
        # marked zip with a non-sidecar extra member is NOT transformed (a
        # corrupt/mis-marked zip is left to normal extraction, not mangled).
        z = _make_zip(['drive.dd.zst', 'unexpected.bin'],
                      comment=BUNDLE_MARKER.encode('cp437'))
        self.assertIsNone(_disk_image_bundle_member(z))

    def test_marker_with_uncompressed_image_rejected(self):
        # A marked zip whose image is a raw (uncompressed) .dd is not a valid
        # bundle image — bundles always carry a compressed image.
        z = _make_zip(['drive.dd', 'drive.map'],
                      comment=BUNDLE_MARKER.encode('cp437'))
        self.assertIsNone(_disk_image_bundle_member(z))


class TestHandleBundle(unittest.TestCase):
    """_handle_disk_image_bundle attaches sidecars as SIDECAR child artefacts of
    the disk image — not as a (collision-prone) synthetic partition."""

    def _run(self):
        calls = {'derived': [], 'seq': []}
        api = types.SimpleNamespace(
            transform_to_disk_image=lambda *a, **k: (
                calls['seq'].append('transform') or {'uuid': 'img'}),
            register_derived_artefact=lambda *a, **k: (
                calls['derived'].append((a, k)) or calls['seq'].append('derived')
                or {'uuid': 'sc'}),
        )
        worker = types.SimpleNamespace(
            api=api,
            fail_analysis=lambda *a, **k: calls.setdefault('fail', (a, k)),
            complete_analysis=lambda *a, **k: calls.setdefault('complete', (a, k)),
        )
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        work_dir = Path(d) / 'work'
        work_dir.mkdir()

        def fake_extract_zip(input_path, out_dir):
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / 'drive.dd.zst').write_bytes(b'image-bytes')
            (out_dir / 'drive.map').write_bytes(b'map')
            (out_dir / 'README.txt').write_bytes(b'hi')
            return {'success': True, 'tool': 'unzip'}

        with patch.object(ext, 'extract_zip', fake_extract_zip):
            ext._handle_disk_image_bundle(
                worker, {'id': 1}, {'uuid': 'art'}, work_dir,
                Path(d) / 'bundle.zip', 'drive.dd.zst')
        return calls

    def test_sidecars_registered_as_sidecar_child_artefacts(self):
        from shared.enums import ArtefactType
        calls = self._run()
        self.assertNotIn('fail', calls)
        # One derived artefact per sidecar (the image itself is NOT re-registered).
        labels = sorted(k['label'] for _a, k in calls['derived'])
        self.assertEqual(labels, ['README.txt', 'drive.map'])
        for _a, k in calls['derived']:
            self.assertEqual(k['artefact_type'], ArtefactType.SIDECAR)
            self.assertIs(k['auto_analyse'], False)

    def test_image_not_registered_as_sidecar(self):
        calls = self._run()
        labels = [k['label'] for _a, k in calls['derived']]
        self.assertNotIn('drive.dd.zst', labels)

    def test_sidecars_registered_before_transform(self):
        # Sidecars must be durably committed BEFORE the irreversible transform,
        # so a crash after the transform (which makes re-runs a no-op) cannot
        # silently drop them.
        calls = self._run()
        self.assertEqual(calls['seq'][-1], 'transform')
        self.assertNotIn('derived', calls['seq'][calls['seq'].index('transform'):][1:])
        self.assertEqual(calls['seq'].count('derived'), 2)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
