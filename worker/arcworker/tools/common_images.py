"""
Common image format conversion tools.

Handles raster images (JPEG, PNG, GIF, BMP, TIFF, WebP, PCX, TGA) via Pillow
and Windows vector metafiles (WMF, EMF) via external tools (wmf2svg, Dexvert emf2svg).
"""

import shutil
import subprocess
from pathlib import Path

from .svg_utils import postprocess_svg

from ..config import log

# Extensions passed through unchanged (browser-native formats)
_PASSTHROUGH_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.webp'})

# Extensions converted to SVG via external tools.
def _wmf_cmd(src: Path, dst: Path) -> list[str]:
    return ['wmf-cli', '--input', str(src), '--output', str(dst)]


def _emf_cmd(src: Path, dst: Path) -> list[str]:
    return ['/opt/dexvert/emf2svg.py', str(src), str(dst)]


_VECTOR_EXTS: dict[str, tuple[tuple[str, object], ...]] = {
    # WMF files are sometimes renamed EMF files
    '.wmf': (('wmf2svg', _wmf_cmd), ('emf2svg', _emf_cmd)),
    # EMF files are sometimes renamed WMF files
    '.emf': (('emf2svg', _emf_cmd), ('wmf2svg', _wmf_cmd)),
}


def _result(success: bool, *, output_path: str | None, fmt: str | None,
            tool: str, error: str | None, **extra) -> dict:
    return {
        'success': success,
        'output_path': output_path,
        'format': fmt,
        'tool': tool,
        'error': error,
        **extra,
    }


def convert_image(input_path: Path, output_dir: Path, analysis_uuid: str) -> dict:
    """
    Convert a common image file to a web-viewable format.

    Browser-native raster formats (JPEG, PNG, GIF, WebP) are copied unchanged.
    Other raster formats (BMP, TIFF, PCX, TGA) are converted to PNG via Pillow.
    WMF and EMF are converted to SVG via wmf2svg / emf2svg respectively.

    Returns a dict with keys:
        success     (bool)
        output_path (str | None)  — absolute path to the output file
        format      (str | None)  — detected format name (e.g. 'PNG', 'WMF')
        tool        (str)         — tool used ('passthrough', 'pillow-convert',
                                   'wmf2svg', 'emf2svg')
        error       (str | None)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = input_path.suffix.lower()

    # --- Vector metafiles ---
    if ext in _VECTOR_EXTS:
        # Try all the converters in order until we hit one that works
        errors = []
        for tool_name, build_cmd in _VECTOR_EXTS[ext]:
            out_svg = output_dir / f'{analysis_uuid}_image.svg'
            cmd = build_cmd(input_path, out_svg)
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=60)
            except FileNotFoundError:
                errors.append(f'{tool_name} not found')
                continue
            except subprocess.TimeoutExpired:
                errors.append(f'{tool_name} timed out')
                continue
            if proc.returncode != 0 or not out_svg.exists():
                out = (proc.stdout + proc.stderr).decode('utf-8', errors='replace').strip()
                errors.append(f'{tool_name} failed (rc={proc.returncode}): {out}')
                log.info(f'Conversion of "{input_path}" via "{cmd}" failed, falling back to next decoder')
                continue

            try:
                postprocess_svg(out_svg)
                exception_trace = None
            except Exception:
                import traceback
                exception_trace = traceback.format_exc()
                log.warning(f'Error cleaning up SVG "{input_path}", bypassing: {exception_trace}')

            return _result(
                True,
                output_path=str(out_svg),
                fmt=ext.lstrip('.').upper(),
                tool=tool_name,
                error=None,
                exception_trace=exception_trace,
            )

        return _result(
            False,
            output_path=None,
            fmt=ext.lstrip('.').upper(),
            tool=_VECTOR_EXTS[ext][0][0],
            error='; '.join(errors) if errors else 'No vector converter succeeded',
        )

    # --- Pass-through raster ---
    if ext in _PASSTHROUGH_EXTS:
        out_path = output_dir / f'{analysis_uuid}_image{ext}'
        shutil.copy2(input_path, out_path)
        return _result(
            True,
            output_path=str(out_path),
            fmt=ext.lstrip('.').upper(),
            tool='passthrough',
            error=None,
        )

    # --- Pillow-converted raster ---
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return _result(
            False,
            output_path=None,
            fmt=None,
            tool='pillow-convert',
            error='Pillow not installed',
        )
    out_path = output_dir / f'{analysis_uuid}_image.png'
    try:
        with Image.open(input_path) as img:
            fmt = img.format or ext.lstrip('.').upper()
            if img.mode not in ('RGB', 'RGBA', 'L', 'P'):
                img = img.convert('RGBA')
            img.save(str(out_path), 'PNG')
    except (OSError, UnidentifiedImageError) as e:
        log.warning('Image conversion failed for %s: %s', input_path, e)
        return _result(
            False,
            output_path=None,
            fmt=None,
            tool='pillow-convert',
            error=str(e),
        )
    return _result(
        True,
        output_path=str(out_path),
        fmt=fmt,
        tool='pillow-convert',
        error=None,
    )

# vim: ts=4 sw=4 et
