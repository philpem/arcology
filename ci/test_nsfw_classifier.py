"""
Tests for the NSFW image classifier (worker/arcworker/tools/nsfw.py).

These tests do not require ONNX models or GPUs — they exercise the
preprocessing, tiling, and classification-logic layers using synthetic
images and lightweight mock sessions.
"""

import io
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Allow running from repo root without installing packages
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.arcworker.tools.nsfw import (
    _build_tiles,
    _open_image,
    _score_tiles,
    classify_batch,
    preprocess,
    softmax,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rgb_image(width, height, color=(128, 128, 128)):
    """Create a tiny PIL Image without touching the filesystem."""
    from PIL import Image
    return Image.new('RGB', (width, height), color)


def _rgba_image(width, height, color=(200, 100, 50, 128)):
    from PIL import Image
    return Image.new('RGBA', (width, height), color)


def _make_meta(input_size=224, nsfw_class_index=1):
    return {
        'nsfw_class_index': nsfw_class_index,
        'input_size': input_size,
        'mean': [0.5, 0.5, 0.5],
        'std':  [0.5, 0.5, 0.5],
        'interpolation': 'bicubic',
        'crop_pct': 1.0,
    }


def _make_sess(score: float, nsfw_class_index: int = 1):
    """Return a mock ONNX session that always outputs *score* for the NSFW class."""
    import numpy as np
    sess = MagicMock()
    # softmax([0, x]) ≈ x for large x, so back-calculate the logit that gives *score*
    # after softmax over 2 classes.  We set logits = [0, log(score/(1-score))].
    safe_score = max(1e-6, min(1 - 1e-6, score))
    logit = float(np.log(safe_score / (1.0 - safe_score)))
    # Output shape: (1, num_classes); class 0 = safe (logit 0), class 1 = nsfw
    output = np.array([[0.0, logit]], dtype=np.float32)
    sess.run.return_value = [output]
    return sess


# ---------------------------------------------------------------------------
# Test: crop-set parity
# ---------------------------------------------------------------------------

class TestCropSetParity(unittest.TestCase):
    """Both stages must always score the same tile set so the colocated gate works."""

    def _run_batch(self, img_width, img_height, size1=384, size2=224):
        """Run a batch with two mock sessions; return (crops1, crops2)."""
        meta1 = _make_meta(input_size=size1)
        meta2 = _make_meta(input_size=size2)
        sess1 = _make_sess(0.50)
        sess2 = _make_sess(0.60)

        # Write image to a temp file classify_batch can open
        import tempfile
        from PIL import Image
        img = Image.new('RGB', (img_width, img_height), (100, 150, 200))
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            img.save(f.name)
            tmp_path = f.name

        results = classify_batch(
            sess1, 'input', meta1,
            sess2, 'input', meta2,
            [tmp_path],
            high_threshold=0.90,
            low_threshold=0.20,
            s2_threshold=0.50,   # low enough to reach stage 2
            s1_min_explicit=0.40,
            agree_threshold=0.55,
            colocated_threshold=0.0,  # disable gate so we can inspect crops
        )
        import os
        os.unlink(tmp_path)
        return results

    def test_small_image_centre_only(self):
        """Images smaller than the trigger produce only 'centre' tile for both stages."""
        # 300×300 < 2*224 = 448 → no grid tiles
        results = self._run_batch(300, 300)
        self.assertEqual(len(results), 1)
        r = results[0]
        s1_names = {c['crop'] for c in r.get('s1_crops', r.get('crops', []))}
        s2_names = {c['crop'] for c in r.get('crops', [])} if r.get('stage') == 2 else s1_names
        self.assertEqual(s1_names, {'centre'})
        if r.get('stage') == 2:
            self.assertEqual(s2_names, {'centre'})

    def test_large_image_matching_tile_sets(self):
        """Images above the trigger threshold produce identical tile sets in both stages."""
        # 1000×1000 >= 2*224 = 448 → grid tiles triggered
        results = self._run_batch(1000, 1000)
        self.assertEqual(len(results), 1)
        r = results[0]
        if r.get('stage') == 2:
            s1_names = {c['crop'] for c in r['s1_crops']}
            s2_names = {c['crop'] for c in r['crops']}
            self.assertEqual(s1_names, s2_names,
                             'Stage-1 and stage-2 must score exactly the same tiles')
            self.assertIn('centre', s1_names)
            # 3×3 grid = 9 tiles + centre = 10
            self.assertGreater(len(s1_names), 1)

    def test_borderline_size_band(self):
        """Images in the 448–767 px band (between s2 and s1 triggers) must still match."""
        # 600×600: >= 2*224 but < 2*384 — previously only stage-2 got grid tiles
        results = self._run_batch(600, 600)
        self.assertEqual(len(results), 1)
        r = results[0]
        if r.get('stage') == 2:
            s1_names = {c['crop'] for c in r['s1_crops']}
            s2_names = {c['crop'] for c in r['crops']}
            self.assertEqual(s1_names, s2_names,
                             'Crop-set mismatch in 448–767 px band: colocated gate would be silently disabled')


# ---------------------------------------------------------------------------
# Test: EXIF orientation
# ---------------------------------------------------------------------------

class TestExifOrientation(unittest.TestCase):
    """_open_image() must apply EXIF rotation so sideways photos score correctly."""

    def _make_rotated_png(self, width, height, orientation: int) -> bytes:
        """Create a minimal PNG with an EXIF orientation tag."""
        from PIL import Image
        import tempfile, os
        img = Image.new('RGB', (width, height), (200, 100, 50))
        # Draw a marker so we can tell orientation was applied
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, width // 4, height], fill=(255, 0, 0))  # red stripe on left

        # Embed EXIF orientation
        import piexif
        exif = piexif.dump({'0th': {piexif.ImageIFD.Orientation: orientation}})
        buf = io.BytesIO()
        img.save(buf, format='JPEG', exif=exif)
        buf.seek(0)
        return buf

    def test_exif_transpose_applied(self):
        """A 90°-rotated image should have its dimensions swapped after _open_image."""
        try:
            import piexif
        except ImportError:
            self.skipTest('piexif not installed')

        # orientation=6 means "rotate 90° CW to display correctly"
        buf = self._make_rotated_png(200, 100, orientation=6)
        from PIL import Image
        raw = Image.open(buf)
        raw_w, raw_h = raw.size  # stored as 200×100

        buf.seek(0)
        corrected = _open_image(buf)
        corr_w, corr_h = corrected.size

        # After applying orientation=6 (90° CW), width and height should swap
        self.assertEqual((corr_w, corr_h), (raw_h, raw_w),
                         'EXIF orientation was not applied: image dimensions unchanged')

    def test_no_exif_unchanged(self):
        """Images without EXIF pass through unchanged."""
        from PIL import Image
        img = Image.new('RGB', (300, 200), (50, 100, 150))
        result = _open_image(img)
        self.assertEqual(result.size, (300, 200))
        self.assertEqual(result.mode, 'RGB')


# ---------------------------------------------------------------------------
# Test: alpha flattening
# ---------------------------------------------------------------------------

class TestAlphaFlattening(unittest.TestCase):
    def test_rgba_flattened_to_rgb(self):
        img = _rgba_image(64, 64, (0, 0, 0, 0))  # fully transparent black
        result = _open_image(img)
        self.assertEqual(result.mode, 'RGB')
        # Fully transparent pixel should become the grey background (128,128,128)
        px = result.getpixel((0, 0))
        self.assertEqual(px, (128, 128, 128))

    def test_opaque_rgba_preserved(self):
        img = _rgba_image(64, 64, (200, 100, 50, 255))  # fully opaque
        result = _open_image(img)
        self.assertEqual(result.mode, 'RGB')
        px = result.getpixel((0, 0))
        self.assertEqual(px, (200, 100, 50))


# ---------------------------------------------------------------------------
# Test: stage-2 error fallback uses calibrated thresholds
# ---------------------------------------------------------------------------

class TestS2ErrorFallback(unittest.TestCase):
    """When stage 2 raises, the fallback must use high_threshold/low_threshold."""

    def _run_with_s2_error(self, score1: float, high=0.90, low=0.20):
        meta1 = _make_meta(input_size=224)
        meta2 = _make_meta(input_size=224)
        sess1 = _make_sess(score1)
        sess2 = MagicMock()
        sess2.run.side_effect = RuntimeError('simulated s2 failure')

        import tempfile
        from PIL import Image
        img = Image.new('RGB', (64, 64), (100, 100, 100))
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            img.save(f.name)
            tmp_path = f.name

        results = classify_batch(
            sess1, 'input', meta1,
            sess2, 'input', meta2,
            [tmp_path],
            high_threshold=high,
            low_threshold=low,
            s2_threshold=0.70,
            s1_min_explicit=0.40,
        )
        import os
        os.unlink(tmp_path)
        return results[0]

    def test_borderline_s2_error_defaults_not_explicit(self):
        """A borderline s1 score that can't be verified by s2 must not be flagged explicit."""
        r = self._run_with_s2_error(score1=0.55)  # in (low=0.20, high=0.90)
        self.assertEqual(r['verdict'], 'not explicit',
                         'Borderline score with s2 error should default to not explicit')
        self.assertTrue(r.get('s2_error'))

    def test_above_high_threshold_s2_error_explicit(self):
        """A score above high_threshold should still be explicit even if s2 errors."""
        r = self._run_with_s2_error(score1=0.95)
        # score1 >= high_threshold → classified at stage 1 before s2 is even called
        self.assertEqual(r['verdict'], 'explicit')

    def test_below_low_threshold_s2_error_not_explicit(self):
        """A score below low_threshold should be not explicit even if s2 errors."""
        r = self._run_with_s2_error(score1=0.10)
        self.assertEqual(r['verdict'], 'not explicit')

    def test_no_stale_0_5_threshold(self):
        """The old fallback used score1 >= 0.5; check that 0.51 is not flipped to explicit."""
        # With old code: 0.51 >= 0.5 → explicit.  With new code: borderline → not explicit.
        r = self._run_with_s2_error(score1=0.51, high=0.90, low=0.20)
        self.assertEqual(r['verdict'], 'not explicit',
                         'Stale 0.5 threshold is still in use in the s2-error fallback')


# ---------------------------------------------------------------------------
# Test: _build_tiles
# ---------------------------------------------------------------------------

class TestBuildTiles(unittest.TestCase):
    def test_small_image_centre_only(self):
        img = _rgb_image(100, 100)
        tiles = _build_tiles(img, trigger_size=224)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0][0], 'centre')

    def test_large_image_grid(self):
        img = _rgb_image(1000, 1000)
        tiles = _build_tiles(img, trigger_size=224)
        names = [n for n, _ in tiles]
        self.assertIn('centre', names)
        # 3×3 grid + centre = 10 tiles (no subdivision at 1000 px for trigger=224, factor=4)
        self.assertEqual(len(tiles), 10)

    def test_very_large_image_subdivided(self):
        # 5000×5000: tile ~ 3333×3333, which is > 224*4=896 → subdivided into 4
        img = _rgb_image(5000, 5000)
        tiles = _build_tiles(img, trigger_size=224, large_threshold_factor=4)
        names = [n for n, _ in tiles]
        # Each of the 9 grid tiles gets split into 4 sub-tiles → 36 + 1 centre = 37
        self.assertIn('centre', names)
        # At least one sub-tile name should exist
        sub_tile_names = [n for n in names if '.' in n]
        self.assertGreater(len(sub_tile_names), 0)

    def test_tile_names_unique(self):
        img = _rgb_image(1000, 1000)
        tiles = _build_tiles(img, trigger_size=224)
        names = [n for n, _ in tiles]
        self.assertEqual(len(names), len(set(names)), 'Tile names must be unique')

    def test_wide_image_tiles(self):
        """A very wide image should still tile correctly."""
        img = _rgb_image(2000, 200)
        tiles = _build_tiles(img, trigger_size=100)
        names = [n for n, _ in tiles]
        self.assertIn('centre', names)
        self.assertGreater(len(names), 1)


# ---------------------------------------------------------------------------
# Test: softmax
# ---------------------------------------------------------------------------

class TestSoftmax(unittest.TestCase):
    def test_sums_to_one(self):
        import numpy as np
        x = np.array([[1.0, 2.0, 3.0]])
        result = softmax(x)
        self.assertAlmostEqual(float(result.sum()), 1.0, places=6)

    def test_numerical_stability(self):
        import numpy as np
        x = np.array([[1000.0, 1001.0]])
        result = softmax(x)
        self.assertTrue(all(np.isfinite(result.flatten())))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
