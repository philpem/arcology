#!/usr/bin/env python3
"""
Export prithivMLmods/Nsfw_Image_Detection_OSS to ONNX and write a preprocessing sidecar.

Model: prithivMLmods/Nsfw_Image_Detection_OSS
Architecture: MetaCLIP-2 vision encoder + classifier, input 224×224 RGB
Output: /onnx/clip/model.onnx     (fp32, opset 18)
        /onnx/clip/clip_meta.json  (preprocessing + class-index metadata)

Run via: python3 export_clip.py
Requires: torch transformers onnx pillow numpy
"""
import json
import torch
from pathlib import Path
from transformers import AutoImageProcessor, AutoModelForImageClassification

MODEL_ID    = "prithivMLmods/Nsfw_Image_Detection_OSS"
OUTPUT_PATH = Path("/onnx/clip/model.onnx")
META_PATH   = Path("/onnx/clip/clip_meta.json")

# Pillow resampling filter integer → name mapping
_PILLOW_FILTER_NAMES = {0: 'nearest', 1: 'lanczos', 2: 'bilinear', 3: 'bicubic', 5: 'hamming'}

print(f"Loading model: {MODEL_ID}")
model = AutoModelForImageClassification.from_pretrained(MODEL_ID)
model.eval()

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Determine NSFW class index ────────────────────────────────────────────────
# prithiv config: id2label = {"0": "SFW", "1": "NSFW"}
cfg      = model.config
id2label = dict(getattr(cfg, 'id2label', {}))
if id2label:
    nsfw_idx = next(
        int(k) for k, v in id2label.items()
        if any(kw in v.lower() for kw in ('nsfw', 'explicit', 'unsafe'))
    )
else:
    raise RuntimeError(f"Cannot determine NSFW class index from config: {cfg}")

# ── Extract preprocessing parameters ─────────────────────────────────────────
# Prefer AutoImageProcessor (carries exact training preprocessing).
# Fall back to standard CLIP/MetaCLIP defaults if processor cannot be loaded.
try:
    processor  = AutoImageProcessor.from_pretrained(MODEL_ID)
    mean       = list(processor.image_mean)
    std        = list(processor.image_std)

    # Derive crop_pct from resize-size vs crop-size relationship.
    # HF CLIPImageProcessor: `size` is the resize target (shortest edge or dict),
    # `crop_size` is the final crop.
    resize_size = getattr(processor, 'size', {})
    crop_size   = getattr(processor, 'crop_size', {})
    if isinstance(resize_size, dict):
        resize_px = resize_size.get('shortest_edge', resize_size.get('height', 224))
    else:
        resize_px = int(resize_size)
    if isinstance(crop_size, dict):
        crop_px = crop_size.get('height', 224)
    elif crop_size:
        crop_px = int(crop_size)
    else:
        crop_px = 224

    crop_pct  = round(crop_px / resize_px, 6) if resize_px else 1.0

    resample_raw = getattr(processor, 'resample', 3)
    interp       = _PILLOW_FILTER_NAMES.get(int(resample_raw), 'bicubic')

    print(
        f"AutoImageProcessor: mean={mean} std={std} "
        f"resize={resize_px} crop={crop_px} crop_pct={crop_pct:.4f} interp={interp}"
    )
except Exception as exc:
    print(f"Warning: AutoImageProcessor failed ({exc}) — using CLIP defaults")
    mean     = [0.48145466, 0.4578275,  0.40821073]
    std      = [0.26862954, 0.26130258, 0.27577711]
    crop_pct = 0.875   # standard CLIP: resize to 256, crop to 224
    interp   = 'bicubic'

meta = {
    'nsfw_class_index': nsfw_idx,
    'input_size':       224,
    'mean':             mean,
    'std':              std,
    'interpolation':    interp,
    'crop_pct':         crop_pct,
}
META_PATH.write_text(json.dumps(meta, indent=2))
print(f"Wrote {META_PATH}: {meta}")

# ── Export to ONNX ────────────────────────────────────────────────────────────
dummy = torch.zeros(1, 3, 224, 224, dtype=torch.float32)

print(f"Exporting to {OUTPUT_PATH} ...")
torch.onnx.export(
    model,
    dummy,
    str(OUTPUT_PATH),
    opset_version=18,
    input_names=["pixel_values"],
    output_names=["logits"],
    dynamic_axes={"pixel_values": {0: "batch"}, "logits": {0: "batch"}},
    dynamo=False,
)
print("Done.")
