#!/usr/bin/env python3
"""
Export Marqo/nsfw-image-detection-384 to ONNX and write a preprocessing sidecar.

Model: Marqo/nsfw-image-detection-384
Architecture: ViT (timm), input 384×384 RGB
Output: /onnx/marqo/model.onnx      (fp32, opset 18)
        /onnx/marqo/marqo_meta.json  (preprocessing + class-index metadata)

Run via: python3 export_marqo.py
Requires: torch transformers timm onnx pillow numpy
"""
import json
import torch
from pathlib import Path
from transformers import AutoModelForImageClassification

MODEL_ID    = "Marqo/nsfw-image-detection-384"
OUTPUT_PATH = Path("/onnx/marqo/model.onnx")
META_PATH   = Path("/onnx/marqo/marqo_meta.json")

print(f"Loading model: {MODEL_ID}")
model = AutoModelForImageClassification.from_pretrained(MODEL_ID)
model.eval()

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Determine NSFW class index ────────────────────────────────────────────────
# Marqo uses a timm ViT config with label_names: ["NSFW", "SFW"]
cfg = model.config
label_names = list(getattr(cfg, 'label_names', []))
id2label    = dict(getattr(cfg, 'id2label', {}))

if label_names:
    nsfw_idx = next(
        i for i, lbl in enumerate(label_names)
        if any(kw in lbl.lower() for kw in ('nsfw', 'explicit', 'unsafe'))
    )
elif id2label:
    nsfw_idx = next(
        int(k) for k, v in id2label.items()
        if any(kw in v.lower() for kw in ('nsfw', 'explicit', 'unsafe'))
    )
else:
    raise RuntimeError(f"Cannot determine NSFW class index from config: {cfg}")

# ── Extract preprocessing parameters ─────────────────────────────────────────
# Marqo's pretrained_cfg carries mean/std/interpolation/crop_pct from timm.
pretrained_cfg = dict(getattr(cfg, 'pretrained_cfg', {}))
mean      = list(pretrained_cfg.get('mean', [0.5, 0.5, 0.5]))
std       = list(pretrained_cfg.get('std',  [0.5, 0.5, 0.5]))
interp    = str(pretrained_cfg.get('interpolation', 'bicubic'))
crop_pct  = float(pretrained_cfg.get('crop_pct', 1.0))

meta = {
    'nsfw_class_index': nsfw_idx,
    'input_size':       384,
    'mean':             mean,
    'std':              std,
    'interpolation':    interp,
    'crop_pct':         crop_pct,
}
META_PATH.write_text(json.dumps(meta, indent=2))
print(f"Wrote {META_PATH}: {meta}")

# ── Export to ONNX ────────────────────────────────────────────────────────────
dummy = torch.zeros(1, 3, 384, 384, dtype=torch.float32)

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
