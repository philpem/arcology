#!/usr/bin/env python3
"""
Pre-process and INT8-quantize the Marqo and CLIP ONNX models.

PyTorch's dynamo-based torch.onnx.export leaves stale ``value_info``
annotations on transformer classifier heads, which trips
``onnx.shape_inference`` when ``quantize_dynamic`` runs.  The workaround
(from the onnxruntime quantization ReadMe) is to run ``quant_pre_process``
first — it uses ORT's symbolic shape inference, strips drifted shape
metadata, and re-infers shapes from scratch.  After that, dynamic INT8
weight-only quantization works.

Reads from /onnx/{marqo,clip}/model.onnx
Writes to /onnx_int8/{marqo,clip}.onnx  (flat, single-file, INT8)

Requires: onnxruntime onnx
"""
from pathlib import Path
from onnxruntime.quantization import quantize_dynamic, QuantType
from onnxruntime.quantization.shape_inference import quant_pre_process

MODELS = [
    (Path("/onnx/marqo/model.onnx"), Path("/onnx_int8/marqo.onnx")),
    (Path("/onnx/clip/model.onnx"),  Path("/onnx_int8/clip.onnx")),
]

SIDECARS = [
    (Path("/onnx/marqo/marqo_meta.json"), Path("/onnx_int8/marqo_meta.json")),
    (Path("/onnx/clip/clip_meta.json"),   Path("/onnx_int8/clip_meta.json")),
]

for src, dst in MODELS:
    dst.parent.mkdir(parents=True, exist_ok=True)
    preprocessed = dst.with_suffix('.preprocessed.onnx')

    print(f"Pre-processing {src} → {preprocessed} ...")
    quant_pre_process(
        input_model_path=str(src),
        output_model_path=str(preprocessed),
        skip_optimization=False,
        skip_onnx_shape=False,
        skip_symbolic_shape=False,
    )

    print(f"Quantizing {preprocessed} → {dst} ...")
    quantize_dynamic(
        str(preprocessed),
        str(dst),
        weight_type=QuantType.QInt8,
    )

    preprocessed.unlink()  # intermediate file, no longer needed

    src_mb = src.stat().st_size / 1024 / 1024
    dst_mb = dst.stat().st_size / 1024 / 1024
    print(f"  {src_mb:.1f} MB → {dst_mb:.1f} MB")

import shutil
for src, dst in SIDECARS:
    shutil.copy(src, dst)
    print(f"Copied {src} → {dst}")

print("Quantization complete.")
