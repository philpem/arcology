#!/usr/bin/env python3
"""
NSFW classifier debug tool.

Runs the two-stage NSFW cascade on one or more images (or directories) and
prints per-image crop breakdowns, threshold sensitivity tables, and summary
statistics useful for dialling in false-positive / false-negative thresholds.

Usage:
    python devtools/nsfw_debug.py [options] <image-or-dir> [...]

Examples:
    # Quick scan of a directory, show only summary
    python devtools/nsfw_debug.py /data/outputs/some_job/

    # Full crop detail for a single image
    python devtools/nsfw_debug.py --crops path/to/image.png

    # Sensitivity sweep: how verdict changes as thresholds vary
    python devtools/nsfw_debug.py --sensitivity /data/outputs/job/

    # JSON output (pipe into jq for filtering)
    python devtools/nsfw_debug.py --json /data/outputs/job/ | jq '.[] | select(.verdict=="explicit")'

    # Use non-default model dir or float32 models
    python devtools/nsfw_debug.py --model-dir /opt/nsfw_models --no-quantize image.png

Options:
    --model-dir DIR     Path to directory containing ONNX + *_meta.json files.
                        Defaults to NSFW_MODEL_DIR env var, then /opt/nsfw_models.
    --no-quantize       Force float32 models even if quantized variants exist.
    --high FLOAT        Stage-1 high threshold (default 0.90).
    --low  FLOAT        Stage-1 low threshold  (default 0.20).
    --s2-threshold FLOAT  Stage-2 conviction threshold (default 0.70).
    --s1-min-explicit FLOAT  Min stage-1 for stage-2 conviction (default 0.40).
    --min-pixels INT    Skip images with area below this (default 16384).
    --crops             Print per-crop score table for every image.
    --sensitivity       Print threshold sensitivity table after results.
    --json              Emit raw JSON result list; skip human-readable output.
    --extensions EXTS   Comma-separated extensions to scan in directories
                        (default: jpg,jpeg,png,gif,bmp,webp,tiff,tif).
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from repo root without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'tif'}


def _find_images(paths: list[str], extensions: set[str]) -> list[Path]:
    found = []
    for p in paths:
        pp = Path(p)
        if pp.is_file():
            found.append(pp)
        elif pp.is_dir():
            for ext in extensions:
                found.extend(sorted(pp.rglob(f'*.{ext}')))
                found.extend(sorted(pp.rglob(f'*.{ext.upper()}')))
        else:
            print(f'[warn] not found: {p}', file=sys.stderr)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for f in found:
        k = str(f.resolve())
        if k not in seen:
            seen.add(k)
            result.append(f)
    return result


def _load_sessions(model_dir: Path, quantize: bool):
    """Load ONNX sessions and metadata, mirroring worker _load_nsfw_sessions logic."""
    try:
        import onnxruntime as ort
    except ImportError:
        print('onnxruntime is not installed. Install with: pip install onnxruntime', file=sys.stderr)
        sys.exit(1)

    suffix = '_quant.onnx' if quantize else '.onnx'

    def _load(stem, fallback_stem=None):
        path = model_dir / (stem + suffix)
        if not path.exists() and fallback_stem:
            path = model_dir / (fallback_stem + suffix)
        if not path.exists():
            # Try without quantize suffix
            path = model_dir / (stem + '.onnx')
        if not path.exists():
            print(f'[error] model not found: {stem}{suffix} in {model_dir}', file=sys.stderr)
            sys.exit(1)
        meta_path = path.with_name(path.stem + '_meta.json')
        if not meta_path.exists():
            # Try sibling meta for quant models (quant model + base meta)
            meta_path = model_dir / (stem + '_meta.json')
        if not meta_path.exists():
            print(f'[error] metadata not found: {meta_path}', file=sys.stderr)
            sys.exit(1)
        sess = ort.InferenceSession(str(path))
        meta = json.loads(meta_path.read_text())
        input_name = sess.get_inputs()[0].name
        print(f'  loaded {path.name}  (input: {input_name}, nsfw_idx: {meta["nsfw_class_index"]})',
              file=sys.stderr)
        return sess, input_name, meta

    print(f'Loading models from {model_dir} (quantize={quantize})...', file=sys.stderr)
    sess1, inp1, meta1 = _load('marqo_nsfw')
    sess2, inp2, meta2 = _load('prithiv_nsfw')
    return sess1, inp1, meta1, sess2, inp2, meta2


def _fmt_pct(v: float) -> str:
    return f'{v * 100:.1f}%'


def _print_crops(label: str, crops: list[dict]) -> None:
    print(f'      {label}:')
    for c in crops:
        bar_len = int(c['score'] * 20)
        bar = '█' * bar_len + '░' * (20 - bar_len)
        print(f'        {c["crop"]:<12} {_fmt_pct(c["score"]):>6}  {bar}')


def _print_result(r: dict, show_crops: bool) -> None:
    verdict_tag = {'explicit': '[EXPLICIT]', 'not explicit': '[clean   ]', 'skipped': '[skipped ]'}.get(
        r['verdict'], f'[{r["verdict"].upper()}]'
    )

    if r['verdict'] == 'skipped':
        print(f'  {verdict_tag}  {r["path"]}  reason={r.get("reason", "?")}')
        return

    stage = r.get('stage', '?')
    score = r.get('score', 0.0)
    s1    = r.get('stage1_score', r.get('score', 0.0))

    if stage == 2:
        score_str = f's1={_fmt_pct(s1)} → s2={_fmt_pct(score)}'
        if r.get('s2_error'):
            score_str += ' [s2-error/fallback]'
    else:
        score_str = f's1={_fmt_pct(score)}'

    wc = r.get('winning_crop', '')
    if wc and wc != 'centre':
        score_str += f'  best-crop={wc}'

    print(f'  {verdict_tag}  {score_str}  {r["path"]}')

    if show_crops:
        if stage == 2:
            _print_crops('stage-1 crops', r.get('s1_crops', []))
            _print_crops('stage-2 crops', r.get('crops', []))
        else:
            _print_crops('stage-1 crops', r.get('crops', []))


def _sensitivity_table(results: list[dict],
                       s2_thresholds: list[float],
                       s1_min_values: list[float]) -> None:
    """Print a grid showing how verdict counts change as thresholds vary."""

    # Only consider images that reached stage 2
    s2_results = [r for r in results if r.get('stage') == 2]
    if not s2_results:
        print('\n[sensitivity] No stage-2 results to analyse.')
        return

    print(f'\n=== Threshold sensitivity ({len(s2_results)} stage-2 images) ===')
    print(f'{"s2_thresh":>10}  {"s1_min":>7}  {"explicit":>10}  {"not_explicit":>13}  {"explicit_paths"}')
    print('-' * 100)

    for s2t in s2_thresholds:
        for s1m in s1_min_values:
            explicit = []
            not_explicit = []
            for r in s2_results:
                s2 = r['score']
                s1 = r.get('stage1_score', 0.0)
                if s2 >= s2t and s1 >= s1m:
                    explicit.append(r['path'])
                else:
                    not_explicit.append(r['path'])
            sample = ', '.join(Path(p).name for p in explicit[:3])
            if len(explicit) > 3:
                sample += f' (+{len(explicit) - 3} more)'
            print(f'{s2t:>10.2f}  {s1m:>7.2f}  {len(explicit):>10}  {len(not_explicit):>13}  {sample}')

    # Show every stage-2 image's raw scores for manual inspection
    print('\n=== Stage-2 raw scores ===')
    print(f'  {"s1":>6}  {"s2":>6}  {"s1_crop":<12}  {"s2_crop":<12}  path')
    print(f'  {"-"*6}  {"-"*6}  {"-"*12}  {"-"*12}  ----')
    for r in sorted(s2_results, key=lambda x: x.get('stage1_score', 0.0), reverse=True):
        s1 = r.get('stage1_score', 0.0)
        s2 = r.get('score', 0.0)
        wc1 = r.get('s1_winning_crop', 'centre')
        wc2 = r.get('winning_crop', 'centre')
        print(f'  {_fmt_pct(s1):>6}  {_fmt_pct(s2):>6}  {wc1:<12}  {wc2:<12}  {r["path"]}')


def main():
    ap = argparse.ArgumentParser(
        description='Debug the two-stage NSFW classifier.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('paths', nargs='+', help='Image files or directories to scan')
    ap.add_argument('--model-dir', default=os.environ.get('NSFW_MODEL_DIR', '/opt/nsfw_models'),
                    help='Directory containing ONNX models + *_meta.json files')
    ap.add_argument('--no-quantize', action='store_true',
                    help='Use float32 models instead of INT8-quantized variants')
    ap.add_argument('--high',  type=float, default=float(os.environ.get('NSFW_HIGH',  '0.90')))
    ap.add_argument('--low',   type=float, default=float(os.environ.get('NSFW_LOW',   '0.20')))
    ap.add_argument('--s2-threshold',    type=float, default=float(os.environ.get('NSFW_S2_THRESHOLD',    '0.70')))
    ap.add_argument('--s1-min-explicit', type=float, default=float(os.environ.get('NSFW_S1_MIN_EXPLICIT', '0.40')))
    ap.add_argument('--min-pixels', type=int, default=int(os.environ.get('NSFW_MIN_PIXELS', '16384')))
    ap.add_argument('--crops',       action='store_true', help='Show per-crop score breakdown')
    ap.add_argument('--sensitivity', action='store_true', help='Print threshold sensitivity table')
    ap.add_argument('--json',        action='store_true', help='Emit raw JSON results')
    ap.add_argument('--extensions',  default=','.join(sorted(_IMAGE_EXTENSIONS)),
                    help='Comma-separated extensions to scan in directories')
    args = ap.parse_args()

    extensions = {e.lstrip('.').lower() for e in args.extensions.split(',')}
    images = _find_images(args.paths, extensions)
    if not images:
        print('No images found.', file=sys.stderr)
        sys.exit(0)

    model_dir = Path(args.model_dir)
    sess1, inp1, meta1, sess2, inp2, meta2 = _load_sessions(model_dir, quantize=not args.no_quantize)

    from worker.arcworker.tools.nsfw import classify_batch

    print(f'\nScanning {len(images)} image(s)...', file=sys.stderr)
    results = classify_batch(
        sess1, inp1, meta1,
        sess2, inp2, meta2,
        [str(p) for p in images],
        high_threshold=args.high,
        low_threshold=args.low,
        min_pixels=args.min_pixels,
        s2_threshold=args.s2_threshold,
        s1_min_explicit=args.s1_min_explicit,
    )

    if args.json:
        print(json.dumps(results, indent=2))
        return

    # Human-readable output
    explicit    = [r for r in results if r.get('verdict') == 'explicit']
    not_exp     = [r for r in results if r.get('verdict') == 'not explicit']
    skipped     = [r for r in results if r.get('verdict') == 'skipped']
    s1_only     = [r for r in results if r.get('stage') == 1]
    s2_reached  = [r for r in results if r.get('stage') == 2]
    quadrant_triggered = [r for r in results
                          if r.get('winning_crop', 'centre') != 'centre'
                          or r.get('s1_winning_crop', 'centre') != 'centre']

    print(f'\n=== Results ({len(results)} images) ===')
    for r in results:
        _print_result(r, show_crops=args.crops)

    print('\n=== Summary ===')
    print(f'  Total:           {len(results)}')
    print(f'  Explicit:        {len(explicit)}')
    print(f'  Not explicit:    {len(not_exp)}')
    print(f'  Skipped:         {len(skipped)}')
    print(f'  Stage-1 only:    {len(s1_only)}')
    print(f'  Stage-2 reached: {len(s2_reached)}')
    if quadrant_triggered:
        print(f'  Quadrant-triggered: {len(quadrant_triggered)} image(s) where a non-centre crop was the winner')

    print('\n  Thresholds used:')
    print(f'    s1_high={args.high}  s1_low={args.low}  s2_threshold={args.s2_threshold}  s1_min_explicit={args.s1_min_explicit}')
    print(f'    min_pixels={args.min_pixels}')

    if args.sensitivity:
        _sensitivity_table(
            results,
            s2_thresholds=[0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90],
            s1_min_values=[0.00, 0.20, 0.30, 0.40, 0.50, 0.60],
        )


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
