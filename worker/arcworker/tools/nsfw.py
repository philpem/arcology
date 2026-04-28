"""
Two-stage ONNX explicit-content image classifier.

Stage 1: Marqo/nsfw-image-detection-384 (ViT, input 384×384)
  - Fast pre-filter.  Scores above high_threshold → explicit immediately.
    Scores below low_threshold → not explicit immediately.  Scores in
    between are passed to stage 2.

Stage 2: prithivMLmods/Nsfw_Image_Detection_OSS (CLIP, input 224×224)
  - Verification stage.  Only invoked for borderline stage-1 scores.

Each model is accompanied by a metadata dict describing its class layout
and preprocessing pipeline:

    {
        "nsfw_class_index": int,   # which output index is the explicit-content class
        "input_size":       int,   # square input size (e.g. 384 or 224)
        "mean":             list,  # per-channel normalisation mean (3 floats)
        "std":              list,  # per-channel normalisation std  (3 floats)
        "interpolation":    str,   # Pillow resample name: 'bicubic', 'lanczos', …
        "crop_pct":         float, # fraction of shorter side used (1.0 = no over-resize)
    }

These dicts are emitted by the export scripts as JSON sidecars alongside the
ONNX files so that runtime inference always uses the training-matched pipeline.

Sessions are intentionally NOT managed here — callers should create them
once and pass them in so models remain loaded across multiple jobs.
"""

from pathlib import Path
import numpy as np


def softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable row-wise softmax."""
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _get_resample_filter(name: str):
    from PIL import Image
    return {
        'lanczos':  Image.LANCZOS,
        'bicubic':  Image.BICUBIC,
        'bilinear': Image.BILINEAR,
        'nearest':  Image.NEAREST,
    }.get(name, Image.BICUBIC)


def preprocess(
    img_or_path,
    size: int,
    mean,
    std,
    resample: str = 'bicubic',
    crop_pct: float = 1.0,
) -> np.ndarray:
    """
    Resize and normalise a PIL Image (or path) to a ``size×size`` float32 array.

    Preserves aspect ratio: the shorter side is scaled to
    ``round(size / crop_pct)`` then the centre ``size×size`` tile is cropped.
    When ``crop_pct == 1.0`` this is simply a shorter-side-to-size resize
    followed by a centre crop with no border.

    Returns a ``(1, 3, H, W)`` array ready for ONNX inference.
    """
    from PIL import Image
    if isinstance(img_or_path, (str, Path)):
        img = Image.open(img_or_path).convert('RGB')
    else:
        img = img_or_path.convert('RGB')

    # Aspect-preserving resize so the shorter side reaches scale_size
    scale_size = max(size, round(size / crop_pct))
    w, h = img.size
    if w <= h:
        new_w, new_h = scale_size, round(h * scale_size / w)
    else:
        new_w, new_h = round(w * scale_size / h), scale_size
    img = img.resize((new_w, new_h), _get_resample_filter(resample))

    # Centre crop to size × size
    left = (new_w - size) // 2
    top  = (new_h - size) // 2
    img  = img.crop((left, top, left + size, top + size))

    arr = np.array(img, dtype=np.float32) / 255.0
    arr = (arr - np.array(mean, dtype=np.float32)) / np.array(std, dtype=np.float32)

    # HWC → CHW → NCHW
    return arr.transpose(2, 0, 1)[np.newaxis]


def _score_image(img, sess, input_name: str, nsfw_idx: int,
                 size: int, mean, std, resample: str, crop_pct: float) -> tuple:
    """Score *img* across centre + optional quadrant crops.

    Returns ``(best_score, winning_crop_name, crop_scores)`` where:
      - ``best_score``       is the highest NSFW probability seen across all crops
      - ``winning_crop_name`` is the name of the crop that produced it
      - ``crop_scores``      is a list of ``{'crop': name, 'score': float}`` for every
                             crop that was scored (always includes 'centre'; includes
                             the four quadrant names when the image is large enough)

    For images whose width or height is >= 2× *size*, five crops are scored:
    the centre crop plus the four quadrant tiles (top-left, top-right,
    bottom-left, bottom-right).  For smaller images only the centre crop is
    scored.
    """
    w, h = img.size
    tile_names = ['centre']
    tiles      = [img]

    if w >= 2 * size or h >= 2 * size:
        half_w, half_h = w // 2, h // 2
        tile_names += ['top-left', 'top-right', 'bottom-left', 'bottom-right']
        tiles += [
            img.crop((0,      0,      half_w, half_h)),
            img.crop((half_w, 0,      w,      half_h)),
            img.crop((0,      half_h, half_w, h)),
            img.crop((half_w, half_h, w,      h)),
        ]

    best         = 0.0
    winning_crop = 'centre'
    crop_scores  = []
    for name, tile in zip(tile_names, tiles, strict=True):
        arr   = preprocess(tile, size, mean, std, resample, crop_pct)
        out   = sess.run(None, {input_name: arr})[0]
        score = float(softmax(out)[0, nsfw_idx])
        crop_scores.append({'crop': name, 'score': score})
        if score > best:
            best         = score
            winning_crop = name

    return best, winning_crop, crop_scores


def classify_batch(
    sess1,
    input_name1: str,
    meta1: dict,
    sess2,
    input_name2: str,
    meta2: dict,
    paths: list[str],
    high_threshold: float,
    low_threshold: float,
    min_pixels: int = 0,
    s2_threshold: float = 0.5,
    s1_min_explicit: float = 0.0,
    agree_threshold: float = 0.55,
    s1_strong: float = 0.75,
) -> list[dict]:
    """
    Classify a list of image paths with the two-stage cascade.

    Args:
        sess1: ONNX InferenceSession for stage-1 model
        input_name1: Input tensor name for sess1
        meta1: Preprocessing/label metadata for sess1 (see module docstring)
        sess2: ONNX InferenceSession for stage-2 model
        input_name2: Input tensor name for sess2
        meta2: Preprocessing/label metadata for sess2
        paths: List of file paths to classify
        high_threshold: Stage-1 score above which result is explicit (no stage 2)
        low_threshold: Stage-1 score below which result is not explicit (no stage 2)
        min_pixels: Skip images whose total pixel area (w×h) is below this (0 = no limit)
        s2_threshold: Stage-2 score that must be reached for an explicit verdict on the primary
            conviction path (default 0.5). Raising this (e.g. to 0.70) reduces false positives
            from stock/artistic photography.
        s1_min_explicit: Minimum stage-1 score for the primary conviction path.
            When score1 < s1_min_explicit, stage-2 can only exonerate (not convict), preventing
            overconfident stage-2 verdicts from overriding a sceptical stage-1 result.
        agree_threshold: Both-models agreement threshold (default 0.55). When both stage-1 and
            stage-2 independently exceed this value, the image is convicted regardless of
            s2_threshold and s1_min_explicit. Catches cases where both models are moderately
            confident but neither reaches the primary-path thresholds.
        s1_strong: Strong stage-1 threshold (default 0.75). When stage-1 is highly confident
            and stage-2 scores above low_threshold (i.e. does not clearly exonerate the image),
            the image is convicted. Catches cases where stage-1 is very confident but stage-2
            is only weakly corroborating.

    Returns:
        List of dicts, one per input path — classified or skipped::

            {
                "path":             str,
                "stage":            1 or 2,          # classified only
                "score":            float,            # classified only; explicit probability (0–1)
                "winning_crop":     str,              # crop name that produced the best score
                "crops":            list[dict],       # [{"crop": name, "score": float}, ...]
                "stage1_score":     float,            # stage-2 results only; stage-1 best score
                "s1_winning_crop":  str,              # stage-2 results only; stage-1 best crop
                "s1_crops":         list[dict],       # stage-2 results only; all stage-1 crop scores
                "conviction_path":  str,              # stage-2 explicit only: "primary"|"agree"|"strong_s1"
                "s2_error":         bool,             # true when stage-2 failed and stage-1 was used as fallback
                "verdict":          "explicit" | "not explicit" | "skipped"
                "reason":           "too_small" | "unreadable"  # skipped only
            }
    """
    from PIL import Image

    nsfw_idx1  = meta1['nsfw_class_index']
    size1      = meta1.get('input_size', 384)
    mean1      = meta1['mean']
    std1       = meta1['std']
    resample1  = meta1.get('interpolation', 'bicubic')
    crop_pct1  = float(meta1.get('crop_pct', 1.0))

    nsfw_idx2  = meta2['nsfw_class_index']
    size2      = meta2.get('input_size', 224)
    mean2      = meta2['mean']
    std2       = meta2['std']
    resample2  = meta2.get('interpolation', 'bicubic')
    crop_pct2  = float(meta2.get('crop_pct', 1.0))

    results = []

    for path in paths:
        path_str = str(path)
        try:
            img = Image.open(path_str)
            w, h = img.size
            if min_pixels > 0 and w * h < min_pixels:
                img.close()
                results.append({'path': path_str, 'verdict': 'skipped', 'reason': 'too_small'})
                continue
            score1, winning_crop1, crops1 = _score_image(
                img, sess1, input_name1, nsfw_idx1, size1, mean1, std1, resample1, crop_pct1,
            )
        except Exception:
            results.append({'path': path_str, 'verdict': 'skipped', 'reason': 'unreadable'})
            continue

        if score1 >= high_threshold:
            results.append({
                'path': path_str, 'stage': 1, 'score': score1, 'verdict': 'explicit',
                'winning_crop': winning_crop1, 'crops': crops1,
            })
            continue

        if score1 <= low_threshold:
            results.append({
                'path': path_str, 'stage': 1, 'score': score1, 'verdict': 'not explicit',
                'winning_crop': winning_crop1, 'crops': crops1,
            })
            continue

        # Borderline: run stage 2
        try:
            score2, winning_crop2, crops2 = _score_image(
                img, sess2, input_name2, nsfw_idx2, size2, mean2, std2, resample2, crop_pct2,
            )
        except Exception:
            verdict2 = 'explicit' if score1 >= 0.5 else 'not explicit'
            results.append({
                'path': path_str, 'stage': 1, 'score': score1, 'verdict': verdict2,
                'winning_crop': winning_crop1, 'crops': crops1, 's2_error': True,
            })
            continue

        # Three independent conviction paths (any one suffices):
        #   primary:  s2 meets the main threshold and s1 is not sceptical
        #   agree:    both models independently score above agree_threshold
        #   strong_s1: stage-1 is highly confident and stage-2 doesn't clearly exonerate
        primary  = score2 >= s2_threshold and score1 >= s1_min_explicit
        agree    = score1 >= agree_threshold and score2 >= agree_threshold
        strong   = score1 >= s1_strong and score2 > low_threshold
        explicit2 = primary or agree or strong

        verdict2 = 'explicit' if explicit2 else 'not explicit'
        entry: dict = {
            'path': path_str, 'stage': 2, 'score': score2, 'verdict': verdict2,
            'winning_crop': winning_crop2, 'crops': crops2,
            'stage1_score': score1, 's1_winning_crop': winning_crop1, 's1_crops': crops1,
        }
        if explicit2:
            entry['conviction_path'] = 'primary' if primary else ('agree' if agree else 'strong_s1')
        results.append(entry)

    return results

# vim: ts=4 sw=4 et
