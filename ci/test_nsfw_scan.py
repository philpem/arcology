"""
Tests for the NSFW classifier (worker/arcworker/tools/nsfw.py).

Covers:
  - preprocess(): shape, aspect-preserving resize, centre crop, normalisation
  - classify_batch(): two-stage cascade, correct class index, pixel-area skip,
    unreadable-image skip, borderline → stage 2 logic, multi-crop for large images
  - softmax applied to raw logits before probability lookup

These tests do NOT require ONNX models — sessions are mocked so they return
pre-configured raw logits.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_nsfw_scan -v
"""

import os
import sys
import tempfile
import unittest
import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

from worker.arcworker.tools.nsfw import classify_batch, preprocess, softmax

# ---------------------------------------------------------------------------
# Shared test metadata dicts (match the shipped model defaults)
# ---------------------------------------------------------------------------
_META1 = {
    'nsfw_class_index': 0,
    'input_size':       384,
    'mean':             [0.5, 0.5, 0.5],
    'std':              [0.5, 0.5, 0.5],
    'interpolation':    'bicubic',
    'crop_pct':         1.0,
}
_META2 = {
    'nsfw_class_index': 1,
    'input_size':       224,
    'mean':             [0.48145466, 0.4578275,  0.40821073],
    'std':              [0.26862954, 0.26130258, 0.27577711],
    'interpolation':    'bicubic',
    'crop_pct':         0.875,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_image_file(width, height, colour=(128, 64, 32)):
    """Return a temp PNG file path of the given size."""
    from PIL import Image
    img = Image.new('RGB', (width, height), colour)
    f = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    img.save(f.name)
    f.close()
    return f.name


def _make_session(logits):
    """Return a mock ONNX session that always returns ``logits`` (a 1-D list)."""
    class _Input:
        name = 'pixel_values'

    class _Session:
        def get_inputs(self_):
            return [_Input()]

        def run(self_, output_names, feed_dict):
            return [np.array([logits], dtype=np.float32)]

    return _Session()


def _make_counting_session(logits):
    """Like _make_session but exposes a ``call_count`` attribute."""
    class _Input:
        name = 'pixel_values'

    class _CountingSession:
        def __init__(self_):
            self_.call_count = 0

        def get_inputs(self_):
            return [_Input()]

        def run(self_, output_names, feed_dict):
            self_.call_count += 1
            return [np.array([logits], dtype=np.float32)]

    return _CountingSession()


def _make_sequence_session(logits_sequence):
    """Return a session whose ``run`` cycles through ``logits_sequence`` in order."""
    class _Input:
        name = 'pixel_values'

    class _SequenceSession:
        def __init__(self_):
            self_._idx = 0

        def get_inputs(self_):
            return [_Input()]

        def run(self_, output_names, feed_dict):
            lgt = logits_sequence[self_._idx % len(logits_sequence)]
            self_._idx += 1
            return [np.array([lgt], dtype=np.float32)]

    return _SequenceSession()


# ---------------------------------------------------------------------------
# preprocess() tests
# ---------------------------------------------------------------------------

class TestPreprocessShape(unittest.TestCase):
    def test_square_input_shape(self):
        from PIL import Image
        img = Image.new('RGB', (200, 200))
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        self.assertEqual(out.shape, (1, 3, 64, 64))

    def test_landscape_input_shape(self):
        from PIL import Image
        img = Image.new('RGB', (400, 200))
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        self.assertEqual(out.shape, (1, 3, 64, 64))

    def test_portrait_input_shape(self):
        from PIL import Image
        img = Image.new('RGB', (200, 400))
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        self.assertEqual(out.shape, (1, 3, 64, 64))

    def test_path_input(self):
        path = _make_image_file(200, 100)
        try:
            out = preprocess(path, size=64, mean=[0.5]*3, std=[0.5]*3)
            self.assertEqual(out.shape, (1, 3, 64, 64))
        finally:
            os.unlink(path)


class TestPreprocessNormalisation(unittest.TestCase):
    def test_all_zero_pixels_normalised(self):
        from PIL import Image
        img = Image.new('RGB', (100, 100), (0, 0, 0))
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        # 0/255 normalised: (0 - 0.5) / 0.5 = -1.0
        self.assertAlmostEqual(float(out[0, 0, 0, 0]), -1.0, places=4)

    def test_all_white_pixels_normalised(self):
        from PIL import Image
        img = Image.new('RGB', (100, 100), (255, 255, 255))
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        # 255/255 normalised: (1 - 0.5) / 0.5 = 1.0
        self.assertAlmostEqual(float(out[0, 0, 0, 0]), 1.0, places=4)

    def test_imagenet_normalisation(self):
        from PIL import Image
        img = Image.new('RGB', (100, 100), (128, 128, 128))
        mean = [0.485, 0.456, 0.406]
        std  = [0.229, 0.224, 0.225]
        out  = preprocess(img, size=64, mean=mean, std=std)
        # channel 0: (128/255 - 0.485) / 0.229 ≈ (0.502 - 0.485) / 0.229 ≈ 0.074
        expected_ch0 = (128.0/255.0 - 0.485) / 0.229
        self.assertAlmostEqual(float(out[0, 0, 0, 0]), expected_ch0, places=3)


class TestPreprocessAspectPreserving(unittest.TestCase):
    """Verify that the shorter side is scaled to size (crop_pct=1.0) and the
    result is a centre crop — no squashing of a wide or tall image."""

    def test_landscape_shorter_side_matches_size(self):
        from PIL import Image
        # Wide image: 200 × 100 (landscape)
        img = Image.new('RGB', (200, 100))
        out = preprocess(img, size=100, mean=[0.5]*3, std=[0.5]*3, crop_pct=1.0)
        self.assertEqual(out.shape, (1, 3, 100, 100))

    def test_portrait_shorter_side_matches_size(self):
        from PIL import Image
        # Tall image: 100 × 200 (portrait)
        img = Image.new('RGB', (100, 200))
        out = preprocess(img, size=100, mean=[0.5]*3, std=[0.5]*3, crop_pct=1.0)
        self.assertEqual(out.shape, (1, 3, 100, 100))

    def test_crop_pct_0875_scale_size(self):
        """crop_pct=0.875 → shorter side scaled to round(100/0.875)=114, crop to 100."""
        from PIL import Image
        img = Image.new('RGB', (200, 100))
        out = preprocess(img, size=100, mean=[0.5]*3, std=[0.5]*3, crop_pct=0.875)
        self.assertEqual(out.shape, (1, 3, 100, 100))

    def test_centre_crop_removes_edges(self):
        """A checkerboard pattern's centre should survive cropping of a landscape image."""
        from PIL import Image
        # Wide image: left half red, right half blue (400 × 200)
        # After resize + centre crop, we expect a mix of red and blue (centre region)
        img = Image.new('RGB', (400, 200))
        for x in range(400):
            for y in range(200):
                img.putpixel((x, y), (255, 0, 0) if x < 200 else (0, 0, 255))
        out = preprocess(img, size=100, mean=[0.0]*3, std=[1.0]*3, crop_pct=1.0)
        # Centre column of output should contain pixels from the horizontal centre
        # of the input.  Neither pure red (left edge) nor pure blue (right edge).
        centre_r = float(out[0, 0, 50, 50])  # channel 0 (red) at centre pixel
        self.assertGreater(centre_r, 0.0,  "centre should not be all-blue")
        self.assertLess(centre_r,    1.0,  "centre should not be all-red")


class TestPreprocessRGBConversion(unittest.TestCase):
    def test_greyscale_to_rgb(self):
        from PIL import Image
        img = Image.new('L', (100, 100), 128)
        out = preprocess(img, size=64, mean=[0.5]*3, std=[0.5]*3)
        self.assertEqual(out.shape, (1, 3, 64, 64))


# ---------------------------------------------------------------------------
# softmax() tests
# ---------------------------------------------------------------------------

class TestSoftmax(unittest.TestCase):
    def test_sum_to_one(self):
        x = np.array([[1.0, 2.0, 3.0]])
        s = softmax(x)
        self.assertAlmostEqual(float(s.sum()), 1.0, places=6)

    def test_monotone(self):
        x = np.array([[1.0, 3.0]])
        s = softmax(x)
        self.assertGreater(float(s[0, 1]), float(s[0, 0]))

    def test_numerically_stable_large_values(self):
        x = np.array([[1000.0, 1001.0]])
        s = softmax(x)
        self.assertFalse(np.any(np.isnan(s)))


# ---------------------------------------------------------------------------
# classify_batch() tests
# ---------------------------------------------------------------------------

class TestClassifyBatchBasic(unittest.TestCase):
    def setUp(self):
        self.path = _make_image_file(300, 300)

    def tearDown(self):
        os.unlink(self.path)

    def test_high_score_explicit_no_stage2(self):
        # Stage-1 logits → softmax → probs[0] = 0.95 (above high=0.90)
        # Logits [5, -5] → softmax ≈ [0.9999, 0.0001]
        sess1 = _make_session([5.0, -5.0])   # probs[0] ≈ 1.0
        sess2 = _make_session([0.0,  0.0])   # would give 0.5 — should not be called
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'explicit')
        self.assertEqual(results[0]['stage'],   1)
        self.assertGreater(results[0]['score'],  0.90)

    def test_low_score_not_explicit_no_stage2(self):
        # Stage-1: probs[0] ≈ 0.0 (below low=0.20) → not explicit
        sess1 = _make_session([-5.0, 5.0])
        sess2 = _make_session([ 0.0, 0.0])
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'not explicit')
        self.assertEqual(results[0]['stage'],   1)
        self.assertLess(results[0]['score'], 0.20)

    def test_borderline_reaches_stage2_explicit(self):
        # Stage-1: probs[0] ≈ 0.5 (borderline)
        # Stage-2: META2 nsfw_class_index = 1, so score2 = probs[1]
        # [-5, 5] → softmax ≈ [0.0001, 0.9999] → probs[1] ≈ 0.9999 → explicit
        sess1 = _make_session([0.0,   0.0])   # probs[0] = 0.5 (borderline for stage 1)
        sess2 = _make_session([-5.0,  5.0])   # probs[1] ≈ 0.9999 → explicit
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'explicit')
        self.assertEqual(results[0]['stage'],   2)

    def test_borderline_reaches_stage2_not_explicit(self):
        sess1 = _make_session([0.0,   0.0])   # probs[0] = 0.5 (borderline)
        sess2 = _make_session([5.0,  -5.0])   # probs[1] ≈ 0.0001 → not explicit
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'not explicit')
        self.assertEqual(results[0]['stage'],   2)

    def test_s2_threshold_blocks_low_s2_score(self):
        # Stage-2 score above 0.5 but below s2_threshold=0.80 → not explicit.
        # softmax([-0.5, 0.5])[1] ≈ 0.731, which is < 0.80
        sess1 = _make_session([0.0, 0.0])      # s1 = 0.5 (borderline)
        sess2 = _make_session([-0.5, 0.5])     # s2 ≈ 0.731 (above 0.5, below 0.80)
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
            s2_threshold=0.80,
        )
        self.assertEqual(results[0]['verdict'], 'not explicit')
        self.assertEqual(results[0]['stage'],   2)

    def test_s1_min_explicit_blocks_low_s1_result(self):
        # Stage-2 says explicit, but stage-1 is below s1_min_explicit → not explicit.
        # _META1 nsfw_class_index=0; softmax([0, 0])[0] = 0.5 < s1_min_explicit=0.60
        sess1 = _make_session([0.0, 0.0])      # s1 = 0.5 < s1_min_explicit=0.60
        sess2 = _make_session([-5.0, 5.0])     # s2 ≈ 0.9999 → would convict without guard
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
            s1_min_explicit=0.60,
        )
        self.assertEqual(results[0]['verdict'], 'not explicit')
        self.assertEqual(results[0]['stage'],   2)

    def test_s1_min_explicit_allows_high_s1_result(self):
        # Stage-1 above s1_min_explicit AND stage-2 above s2_threshold → explicit.
        # _META1 nsfw_class_index=0; softmax([1, 0])[0] ≈ 0.731 > s1_min_explicit=0.60
        sess1 = _make_session([1.0, 0.0])      # s1 ≈ 0.731 > s1_min_explicit=0.60
        sess2 = _make_session([-5.0, 5.0])     # s2 ≈ 0.9999
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
            s1_min_explicit=0.60,
        )
        self.assertEqual(results[0]['verdict'], 'explicit')
        self.assertEqual(results[0]['stage'],   2)

    def test_empty_paths_returns_empty(self):
        sess1 = _make_session([0.0, 0.0])
        sess2 = _make_session([0.0, 0.0])
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(results, [])

    def test_result_contains_path_and_score(self):
        sess1 = _make_session([5.0, -5.0])
        sess2 = _make_session([0.0,  0.0])
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertIn('path',    results[0])
        self.assertIn('score',   results[0])
        self.assertIn('verdict', results[0])
        self.assertIn('stage',   results[0])


class TestClassifyBatchNsfwClassIndex(unittest.TestCase):
    """Verify that nsfw_class_index is respected — this was the critical bug."""

    def setUp(self):
        self.path = _make_image_file(300, 300)

    def tearDown(self):
        os.unlink(self.path)

    def test_idx0_high_score_is_explicit(self):
        # probs = softmax([5, -5]) ≈ [0.9999, 0.0001]
        # With nsfw_class_index=0: score = probs[0] ≈ 0.9999 → explicit
        meta = dict(_META1, nsfw_class_index=0)
        sess1 = _make_session([5.0, -5.0])
        sess2 = _make_session([0.0,  0.0])
        results = classify_batch(
            sess1, 'pixel_values', meta,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(results[0]['verdict'], 'explicit')

    def test_idx1_high_score_is_explicit(self):
        # With nsfw_class_index=1 and same logits: score = probs[1] ≈ 0.0001 → not explicit
        meta = dict(_META1, nsfw_class_index=1)
        sess1 = _make_session([5.0, -5.0])
        sess2 = _make_session([0.0,  0.0])
        results = classify_batch(
            sess1, 'pixel_values', meta,
            sess2, 'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        # probs[1] ≈ 0.0001 is below low_threshold=0.20
        self.assertEqual(results[0]['verdict'], 'not explicit')

    def test_inverted_index_produces_opposite_verdict(self):
        # Demonstrate the original bug: using index 1 when 0 is NSFW gives wrong result.
        # Logits [5, -5] → probs ≈ [0.9999, 0.0001]
        # Correct (idx=0): explicit.  Wrong (idx=1): not explicit.
        logits = [5.0, -5.0]
        sess1_correct = _make_session(logits)
        sess2 = _make_session([0.0, 0.0])

        correct_meta = dict(_META1, nsfw_class_index=0)
        res_correct  = classify_batch(
            sess1_correct, 'pixel_values', correct_meta,
            sess2,         'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )

        sess1_wrong = _make_session(logits)
        wrong_meta  = dict(_META1, nsfw_class_index=1)
        res_wrong   = classify_batch(
            sess1_wrong, 'pixel_values', wrong_meta,
            sess2,       'pixel_values', _META2,
            [self.path], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(res_correct[0]['verdict'], 'explicit')
        self.assertEqual(res_wrong[0]['verdict'],   'not explicit')


class TestClassifyBatchEdgeCases(unittest.TestCase):
    def test_unreadable_image_skipped(self):
        """A corrupt file produces a skipped entry with reason='unreadable'."""
        f = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        f.write(b'this is not an image')
        f.close()
        try:
            sess1 = _make_session([0.0, 0.0])
            sess2 = _make_session([0.0, 0.0])
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                sess2, 'pixel_values', _META2,
                [f.name], high_threshold=0.90, low_threshold=0.20,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['verdict'], 'skipped')
            self.assertEqual(results[0]['reason'],  'unreadable')
        finally:
            os.unlink(f.name)

    def test_missing_file_skipped(self):
        """A missing file produces a skipped entry with reason='unreadable'."""
        sess1 = _make_session([0.0, 0.0])
        sess2 = _make_session([0.0, 0.0])
        results = classify_batch(
            sess1, 'pixel_values', _META1,
            sess2, 'pixel_values', _META2,
            ['/nonexistent/path/image.png'], high_threshold=0.90, low_threshold=0.20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'skipped')
        self.assertEqual(results[0]['reason'],  'unreadable')

    def test_min_pixels_skip(self):
        """Images below min_pixels produce a skipped entry with reason='too_small'."""
        path = _make_image_file(32, 32)  # 1024 pixels
        try:
            sess1 = _make_session([5.0, -5.0])
            sess2 = _make_session([0.0,  0.0])
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                sess2, 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20, min_pixels=4096,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['verdict'], 'skipped')
            self.assertEqual(results[0]['reason'],  'too_small')
        finally:
            os.unlink(path)

    def test_min_pixels_zero_no_skip(self):
        path = _make_image_file(32, 32)
        try:
            sess1 = _make_session([5.0, -5.0])
            sess2 = _make_session([0.0,  0.0])
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                sess2, 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20, min_pixels=0,
            )
            self.assertEqual(len(results), 1)
        finally:
            os.unlink(path)

    def test_wide_frame_passes_area_test(self):
        """A wide-but-short image that exceeds the area threshold is not skipped.

        A per-dimension test (min_size=64) would incorrectly skip a 200×50
        frame because height=50 < 64, even though 200×50=10000 pixels is
        enough for a meaningful classification.
        """
        path = _make_image_file(200, 50)  # 10000 pixels — above threshold
        try:
            sess1 = _make_session([5.0, -5.0])
            sess2 = _make_session([0.0,  0.0])
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                sess2, 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20, min_pixels=4096,
            )
            self.assertEqual(len(results), 1)
        finally:
            os.unlink(path)

    def test_multiple_images_ordered(self):
        p1 = _make_image_file(300, 300, colour=(255, 0, 0))
        p2 = _make_image_file(300, 300, colour=(0, 255, 0))
        try:
            sess1 = _make_session([5.0, -5.0])   # explicit for all
            sess2 = _make_session([0.0,  0.0])
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                sess2, 'pixel_values', _META2,
                [p1, p2], high_threshold=0.90, low_threshold=0.20,
            )
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]['path'], p1)
            self.assertEqual(results[1]['path'], p2)
        finally:
            os.unlink(p1)
            os.unlink(p2)


class TestMultiCrop(unittest.TestCase):
    """Verify multi-crop inference for large images."""

    def test_small_image_single_crop(self):
        """An image smaller than 2× model input size is scored with one crop."""
        # _META1 input_size=384; 2×384=768; image 300×300 — both below threshold
        sess1 = _make_counting_session([5.0, -5.0])
        path  = _make_image_file(300, 300)
        try:
            classify_batch(
                sess1, 'pixel_values', _META1,
                _make_session([0.0, 0.0]), 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20,
            )
        finally:
            os.unlink(path)
        self.assertEqual(sess1.call_count, 1)

    def test_large_image_five_crops(self):
        """An image with width >= 2× model input size is scored with five crops."""
        # _META1 input_size=384; 2×384=768; image 800×600 — width 800 >= 768
        sess1 = _make_counting_session([5.0, -5.0])
        path  = _make_image_file(800, 600)
        try:
            classify_batch(
                sess1, 'pixel_values', _META1,
                _make_session([0.0, 0.0]), 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20,
            )
        finally:
            os.unlink(path)
        self.assertEqual(sess1.call_count, 5)

    def test_tall_image_five_crops(self):
        """An image with height >= 2× model input size is scored with five crops."""
        # _META1 input_size=384; 2×384=768; image 400×800 — height 800 >= 768
        sess1 = _make_counting_session([5.0, -5.0])
        path  = _make_image_file(400, 800)
        try:
            classify_batch(
                sess1, 'pixel_values', _META1,
                _make_session([0.0, 0.0]), 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20,
            )
        finally:
            os.unlink(path)
        self.assertEqual(sess1.call_count, 5)

    def test_corner_content_flagged(self):
        """Explicit content that appears in only one quadrant tile is still caught.

        Centre crop → safe (logits [-5, 5] → probs[0] ≈ 0.0001).
        Top-left tile → explicit (logits [5, -5] → probs[0] ≈ 0.9999).
        Other tiles → safe.  Maximum score drives verdict to 'explicit'.
        """
        logits_seq = [
            [-5.0,  5.0],  # centre: safe
            [ 5.0, -5.0],  # top-left: explicit
            [-5.0,  5.0],  # top-right: safe
            [-5.0,  5.0],  # bottom-left: safe
            [-5.0,  5.0],  # bottom-right: safe
        ]
        sess1 = _make_sequence_session(logits_seq)
        path  = _make_image_file(800, 600)
        try:
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                _make_session([0.0, 0.0]), 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20,
            )
        finally:
            os.unlink(path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'explicit')
        self.assertGreater(results[0]['score'], 0.90)

    def test_all_safe_crops_not_explicit(self):
        """An image where every crop is safe is correctly classified as not explicit."""
        sess1 = _make_counting_session([-5.0, 5.0])  # all crops → probs[0] ≈ 0
        path  = _make_image_file(800, 600)
        try:
            results = classify_batch(
                sess1, 'pixel_values', _META1,
                _make_session([0.0, 0.0]), 'pixel_values', _META2,
                [path], high_threshold=0.90, low_threshold=0.20,
            )
        finally:
            os.unlink(path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['verdict'], 'not explicit')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
