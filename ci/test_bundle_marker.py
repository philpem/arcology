"""
Tests that the CLI's local bundle-marker / sidecar definitions stay in sync with
the worker/web source of truth in ``shared/bundle.py`` (the ``arco`` package is
installed standalone and cannot import ``shared`` at runtime, so the constants
are duplicated and drift-checked here), and that the marker the CLI writes into a
bundle zip round-trips through the worker's ``read_zip_comment``.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

import shared.bundle as shared_bundle  # noqa: E402
from cli.arccli.commands import bulk_import as cli  # noqa: E402
from worker.arcworker.tools.archives import read_zip_comment  # noqa: E402


class TestMarkerDrift(unittest.TestCase):
    def test_marker_matches_shared(self):
        self.assertEqual(cli._BUNDLE_MARKER, shared_bundle.BUNDLE_MARKER)

    def test_sidecar_rules_match_shared(self):
        self.assertEqual(cli._SIDECAR_NAME_PREFIXES, shared_bundle.SIDECAR_NAME_PREFIXES)
        self.assertEqual(cli._SIDECAR_EXTENSIONS, shared_bundle.SIDECAR_EXTENSIONS)


class TestMarkerRoundTrip(unittest.TestCase):
    def test_cli_bundle_comment_reads_back(self):
        # Use an already-compressed image so the bundle is built (stored) without
        # needing the zstandard library — this test is about the marker, not zstd.
        d = tempfile.mkdtemp()
        (Path(d) / 'drive.dd.zst').write_bytes(b'\x00' * 20000)
        (Path(d) / 'drive.map').write_bytes(b'map')
        sidecars = cli._find_sidecars(Path(d) / 'drive.dd.zst', 'drive')
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = cli._build_sidecar_bundle(
                Path(d) / 'drive.dd.zst', sidecars, 'drive', tmp)
            self.assertEqual(read_zip_comment(zip_path), shared_bundle.BUNDLE_MARKER)

    def test_marker_is_pure_ascii(self):
        # Must round-trip unchanged through cp437 (the worker's decode).
        marker = shared_bundle.BUNDLE_MARKER
        self.assertEqual(marker.encode('cp437').decode('cp437'), marker)


class TestIsSidecarName(unittest.TestCase):
    def test_base_name_match(self):
        self.assertTrue(shared_bundle.is_sidecar_name('drive.map', 'drive'))
        self.assertTrue(shared_bundle.is_sidecar_name('drive.dd.txt', 'drive'))

    def test_generic_readme_and_checksums(self):
        for name in ('README.txt', 'CHECKSUMS', 'image.md5', 'SHA256SUMS'):
            self.assertTrue(shared_bundle.is_sidecar_name(name, 'drive'), name)

    def test_non_sidecar(self):
        for name in ('other.dd', 'random.bin', 'photo.jpg'):
            self.assertFalse(shared_bundle.is_sidecar_name(name, 'drive'), name)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
