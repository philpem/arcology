"""
Unit tests for common image format conversion (images_common.py).

Tests pass-through of browser-native formats, Pillow-based raster conversion,
and graceful failure when input is corrupt or external tools are absent.

WMF/EMF tests only run when the respective external tools are installed;
they are skipped otherwise.

Environment variables:
    WORKER_API_KEY — required by worker config (defaults to 'test')

Run:
    python -m unittest ci.test_image_conversion -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


try:
    from PIL import Image
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False

_HAS_WMF2SVG = shutil.which('wmf2svg') is not None
_HAS_EMF2SVG = Path('/opt/dexvert/emf2svg.py').exists()


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
        from worker.arcworker.tools.images_common import convert_image
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

    # --- Pass-through by content sniff (RISC OS filetype-suffix names) ---

    def test_jpeg_riscos_suffix_passthrough(self):
        # RISC OS extracts name JPEGs 'Photo,c85' (no '.jpg' extension).
        # The bytes are a real JPEG, so they must be passed through unchanged
        # rather than re-encoded to PNG.
        src = self.tmpdir / 'Photo,c85'
        _make_jpeg(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        self.assertEqual(result['format'], 'JPG')
        out = Path(result['output_path'])
        self.assertEqual(out.suffix, '.jpg')
        self.assertEqual(out.read_bytes(), original_bytes)

    def test_png_riscos_suffix_passthrough(self):
        src = self.tmpdir / 'Image,b60'  # RISC OS PNG filetype
        _make_png(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        out = Path(result['output_path'])
        self.assertEqual(out.suffix, '.png')
        self.assertEqual(out.read_bytes(), original_bytes)

    def test_gif_riscos_suffix_passthrough(self):
        src = self.tmpdir / 'Anim,695'  # RISC OS GIF filetype
        _make_gif(src)
        original_bytes = src.read_bytes()
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'passthrough')
        out = Path(result['output_path'])
        self.assertEqual(out.suffix, '.gif')
        self.assertEqual(out.read_bytes(), original_bytes)

    def test_bmp_riscos_suffix_still_converts_to_png(self):
        # BMP is not browser-native; even without a '.bmp' extension it must
        # still be converted to PNG (the magic sniff only passes through
        # browser-native formats).
        src = self.tmpdir / 'Picture,69c'  # RISC OS BMP filetype
        _make_bmp(src)
        result = self._convert(src)
        self.assertTrue(result['success'])
        self.assertEqual(result['tool'], 'pillow-convert')
        out = Path(result['output_path'])
        self.assertEqual(out.suffix, '.png')
        with Image.open(out) as img:
            self.assertEqual(img.format, 'PNG')

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

    def test_pillow_codec_integer_error_is_translated(self):
        # Simulate the OSError(-2) that Ubuntu's Pillow raises when the C
        # codec fails (e.g. malformed TIFF missing StripByteCounts).
        # Patch Image.open so it raises OSError(-2) and verify the stored
        # error is human-readable rather than just "-2".
        mock_img = MagicMock()
        mock_img.__enter__ = lambda s: s
        mock_img.__exit__ = MagicMock(return_value=False)
        mock_img.format = 'TIFF'
        mock_img.mode = 'RGB'
        mock_img.save.side_effect = OSError(-2)
        src = self.tmpdir / 'broken.tif'
        src.write_bytes(b'II\x2a\x00')  # minimal TIFF header stub
        with patch('PIL.Image.open', return_value=mock_img):
            result = self._convert(src)
        self.assertFalse(result['success'])
        error = result['error']
        self.assertNotEqual(error, '-2', 'bare "-2" should be translated')
        self.assertIn('-2', error)
        self.assertIn('decoding error', error)

    # --- WMF / EMF missing-tool handling ---

    @unittest.skipIf(_HAS_WMF2SVG, 'wmf2svg is installed — skipping missing-tool test')
    def test_wmf_missing_tool_returns_failure(self):
        src = self.tmpdir / 'test.wmf'
        src.write_bytes(b'\xd7\xcd\xc6\x9a\x00\x00')  # WMF magic bytes
        result = self._convert(src)
        self.assertFalse(result['success'])
        self.assertIn('wmf2svg', result.get('error', ''))

    @unittest.skipIf(_HAS_EMF2SVG, 'emf2svg is installed — skipping missing-tool test')
    def test_emf_missing_tool_returns_failure(self):
        src = self.tmpdir / 'test.emf'
        src.write_bytes(b'\x01\x00\x00\x00')  # EMF magic bytes
        result = self._convert(src)
        self.assertFalse(result['success'])
        self.assertIn('emf2svg', result.get('error', ''))


class TestFormatConvertExtractionScan(unittest.TestCase):

    def test_resolved_display_path_does_not_need_to_match_selected_path(self):
        from worker.arcworker.analyses import images
        from worker.arcworker.analyses._common import BatchScanResult

        tmpdir = Path(tempfile.mkdtemp())
        try:
            source = tmpdir / 'Boot,fff'
            source.write_text('print "hello"')
            file_data = {
                'path': '!HDBackup/!Boot',
                'filename': '!Boot',
                'risc_os_filetype': 'fff',
            }

            def fake_scan(worker, analysis, artefact, *, select_files):
                self.assertTrue(select_files(file_data))
                return BatchScanResult(
                    extraction_path='extract-root',
                    files=[file_data],
                    path_prefix='',
                    partition_uuid='partition-uuid',
                )

            def fake_iter(worker, files, extraction_path, work_dir, *, path_prefix='', on_missing=None):
                yield files[0], source, '2F8B1A7A'

            worker = MagicMock()
            worker._convert_file_to_outputs.return_value = ([{'path': 'converted.txt'}], None, [])
            analysis = {'id': 123, 'uuid': 'analysis-uuid'}
            artefact = {'artefact_type': 'raw_sector', 'uuid': 'art', 'label': 'disc'}

            with patch.object(images, 'scan_partition_files', side_effect=fake_scan), \
                    patch.object(images, 'iter_resolved_files', side_effect=fake_iter):
                images.process_format_convert(worker, analysis, artefact, tmpdir)

            worker.fail_analysis.assert_not_called()
            worker.complete_analysis.assert_called_once()
            details = worker.complete_analysis.call_args.kwargs['details']
            self.assertIn('"source_file": "2F8B1A7A"', details)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
