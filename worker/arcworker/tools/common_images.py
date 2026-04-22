"""
Common image format conversion tools.

Handles raster images (JPEG, PNG, GIF, BMP, TIFF, WebP, PCX, TGA) via Pillow
and Windows vector metafiles (WMF, EMF) via external tools (wmf2svg, emf2svg-conv).
"""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .svg_utils import postprocess_svg

from ..config import log

# Extensions passed through unchanged (browser-native formats)
_PASSTHROUGH_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.webp'})

# Extensions converted to SVG via external tools: ext → (tool_name, build_cmd_fn)
def _wmf_cmd(src: Path, dst: Path) -> list[str]:
    return ['wmf2svg', '-o', str(dst), str(src)]

def _emf_cmd(src: Path, dst: Path) -> list[str]:
    return ['/opt/dexvert/emf2svg.py', str(src), str(dst)]

_VECTOR_EXTS: dict[str, tuple[str, object]] = {
    # WMF files are sometimes renamed EMF files
    '.wmf': (_wmf_cmd, _emf_cmd),
    # EMF files are sometimes renamed WMF files
    '.emf': (_emf_cmd, _wmf_cmd),
}

def _ensure_svg_namespace(svg_path: Path) -> None:
    """
    Ensure the attached SVG file has a valid namespace
    """
    SVG_NS = "http://www.w3.org/2000/svg"

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # ElementTree represents tags as {namespace}tag if namespaced
        if not root.tag.startswith("{"):
            # no default SVG namespace, add it
            root.set("xmlns", SVG_NS)
            tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    except ET.ParseError:
        # if it's not well-formed XML, rethrow
        # TODO, need to raise a better error
        raise


def convert_image(input_path: Path, output_dir: Path, analysis_uuid: str) -> dict:
    """
    Convert a common image file to a web-viewable format.

    Browser-native raster formats (JPEG, PNG, GIF, WebP) are copied unchanged.
    Other raster formats (BMP, TIFF, PCX, TGA) are converted to PNG via Pillow.
    WMF and EMF are converted to SVG via wmf2svg / emf2svg-conv respectively.

    Returns a dict with keys:
        success     (bool)
        output_path (str | None)  — absolute path to the output file
        format      (str | None)  — detected format name (e.g. 'PNG', 'WMF')
        tool        (str)         — tool used ('passthrough', 'pillow-convert',
                                   'wmf2svg', 'emf2svg-conv')
        error       (str | None)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = input_path.suffix.lower()

    # --- Vector metafiles ---
    err = None
    if ext in _VECTOR_EXTS:
        # Get the list of possible converters for this type
        build_cmd_l = _VECTOR_EXTS[ext]
        if type(build_cmd_l) not in (list,tuple):
            build_cmd_l = (build_cmd_l)
        
        # Try all the converters in order until we hit one that works
        for build_cmd in build_cmd_l:
            out_svg = output_dir / f'{analysis_uuid}_image.svg'
            cmd = build_cmd(input_path, out_svg)
            tool_name = cmd[0]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=60)
            except FileNotFoundError:
                err = {
                    'success': False, 'output_path': None,
                    'format': ext.lstrip('.').upper(), 'tool': tool_name,
                    'error': f'{tool_name!r} not found — install {tool_name} in the worker',
                }
                continue
            except subprocess.TimeoutExpired:
                err = {
                    'success': False, 'output_path': None,
                    'format': ext.lstrip('.').upper(), 'tool': tool_name,
                    'error': f'{tool_name} timed out',
                }
                continue
            if proc.returncode != 0 or not out_svg.exists():
                out = (proc.stdout + proc.stderr).decode('utf-8', errors='replace').strip()
                err = {
                    'success': False, 'output_path': None,
                    'format': ext.lstrip('.').upper(), 'tool': tool_name,
                    'error': f'{tool_name} failed (rc={proc.returncode}): {out}',
                }
                log.info(f'Conversion of "{input_path}" via "{cmd}" failed, falling back to next decoder')
                continue

            # For wmf2svg: tidy up the SVG output so it's relatively standards-compliant.
            # For everything else: tidy up the SVG output with Scour.
            try:
                postprocess_svg(out_svg)
                exception_trace = None
            except:
                import traceback
                exception_trace = traceback.format_exc()
                log.warning(f'Error cleaning up SVG "{input_path}", bypassing: {exception_trace}')

            return {
                'success': True, 'output_path': str(out_svg),
                'format': ext.lstrip('.').upper(), 'tool': tool_name,
                'error': None,
                'exception_trace': exception_trace,
            }

        if err is not None:
            # Some kind of error, return it
            return err

    # --- Pass-through raster ---
    if ext in _PASSTHROUGH_EXTS:
        out_path = output_dir / f'{analysis_uuid}_image{ext}'
        shutil.copy2(input_path, out_path)
        return {
            'success': True, 'output_path': str(out_path),
            'format': ext.lstrip('.').upper(), 'tool': 'passthrough',
            'error': None,
        }

    # --- Pillow-converted raster ---
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return {
            'success': False, 'output_path': None, 'format': None,
            'tool': 'pillow-convert',
            'error': 'Pillow not installed',
        }
    out_path = output_dir / f'{analysis_uuid}_image.png'
    try:
        with Image.open(input_path) as img:
            fmt = img.format or ext.lstrip('.').upper()
            if img.mode not in ('RGB', 'RGBA', 'L', 'P'):
                img = img.convert('RGBA')
            img.save(str(out_path), 'PNG')
    except (OSError, UnidentifiedImageError) as e:
        log.warning('Image conversion failed for %s: %s', input_path, e)
        return {
            'success': False, 'output_path': None, 'format': None,
            'tool': 'pillow-convert', 'error': str(e),
        }
    return {
        'success': True, 'output_path': str(out_path),
        'format': fmt, 'tool': 'pillow-convert',
        'error': None,
    }

# vim: ts=4 sw=4 et
