"""
Unit tests for common image format conversion (common_images.py).

Tests pass-through of browser-native formats, Pillow-based raster conversion,
and graceful failure when input is corrupt or external tools are absent.

WMF/EMF tests only run when the respective external tools are installed;
they are skipped otherwise.

Environment variables:
    WORKER_API_KEY — required by worker config (defaults to 'test')

Run:
    python -m unittest ci.test_image_conversion -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

try:
    from PIL import Image
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False

_HAS_WMF2SVG   = shutil.which('wmf2svg')   is not None
_HAS_EMF2SVG   = shutil.which('emf2svg-conv') is not None


def _make_png(path: Path, size=(10, 10)) -> None:
    img = Image.new('RGB', size, color=(255, 0, 0))
    img.save(str(path), 'PNG')


def _make_jpeg(path: Path, size=(10, 10)) -> None:
    img = Image.new('RGB', size, color=(0, 255, 0))
    img.save(str(path), 'JPEG')


def _make_gif(path: Path, size=(10, 10)) -> None:
    img = Image.new('P', size)
    img.save(str(path), 'GIF')


def _make_bmp(path: Path, size=(10, 10)) -> None:
    img = Image.new('RGB', size, color=(0, 0, 255))
    img.save(str(path), 'BMP')


def _make_tiff(path: Path, size=(10, 10)) -> None:
    img = Image.new('RGB', size, color=(128, 128, 128))
    img.save(str(path), 'TIFF')


@unittest.skipUnless(_HAS_PILLOW, 'Pillow not installed')
class TestCommonImageConversion(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.outdir = self.tmpdir / 'out'
        self.outdir.mkdir()
        self.uuid = 'testuuid1234'

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _convert(self, src: Path) -> dict:
        from worker.arcworker.tools.common_images import convert_image
        return convert_image(src, self.outdir, self.uuid)

    # --- Pass-through tests ---

    def test_png_passthrough(self):
        src = self.tmpdir / 'test.png'
        _make_png(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        self.assertEqual(Path(result['output_path']).read_bytes(), original_bytes)

    def test_jpeg_passthrough(self):
        src = self.tmpdir / 'test.jpg'
        _make_jpeg(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        self.assertEqual(Path(result['output_path']).read_bytes(), original_bytes)

    def test_gif_passthrough(self):
        src = self.tmpdir / 'test.gif'
        _make_gif(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        self.assertEqual(Path(result['output_path']).read_bytes(), original_bytes)

    # --- Pillow-convert tests ---

    def test_bmp_converts_to_png(self):
        src = self.tmpdir / 'test.bmp'
        _make_bmp(src)
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'pillow-convert')
        out = Path(result['output_path'])
        self.assertTrue(out.exists())
        with Image.open(out) as img:
            self.assertEqual(img.format, 'PNG')

    def test_tiff_converts_to_png(self):
        src = self.tmpdir / 'test.tiff'
        _make_tiff(src)
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'pillow-convert')
        out = Path(result['output_path'])
        with Image.open(out) as img:
            self.assertEqual(img.format, 'PNG')

    # --- Error handling ---

    def test_corrupt_file_returns_failure(self):
        src = self.tmpdir / 'corrupt.bmp'
        src.write_bytes(b'\x00\x01\x02\x03garbage data that is not a valid image')
        result = self._convert(src)
        self.assertFalse(result['success'])
        self.assertIsNotNone(result['error'])

    # --- WMF / EMF missing-tool handling ---

    @unittest.skipIf(_HAS_WMF2SVG, 'wmf2svg is installed — skipping missing-tool test')
    def test_wmf_missing_tool_returns_failure(self):
        src = self.tmpdir / 'test.wmf'
        src.write_bytes(b'\xd7\xcd\xc6\x9a\x00\x00')  # WMF magic bytes
        result = self._convert(src)
        self.assertFalse(result['success'])
        self.assertIn('wmf2svg', result.get('error', ''))

    @unittest.skipIf(_HAS_EMF2SVG, 'emf2svg-conv is installed — skipping missing-tool test')
    def test_emf_missing_tool_returns_failure(self):
        src = self.tmpdir / 'test.emf'
        src.write_bytes(b'\x01\x00\x00\x00')  # EMF magic bytes
        result = self._convert(src)
        self.assertFalse(result['success'])
        self.assertIn('emf2svg-conv', result.get('error', ''))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
