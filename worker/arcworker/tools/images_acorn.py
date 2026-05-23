"""
Acorn/RISC OS image conversion tools.

Provides wrappers for converting Acorn Sprite and Draw files to portable formats.
"""

import io
import re
import traceback as _tb
from pathlib import Path
from ..config import log
from .base import tool_result


def _safe_sprite_name(name: str, index: int) -> str:
    """Return a filesystem-safe version of a sprite name."""
    safe = re.sub(r'[^\w\-]', '_', name)
    if not safe or safe.strip('_') == '':
        safe = f'sprite{index:02d}'
    return safe[:64]



def convert_sprite(input_path: Path, output_dir: Path, analysis_uuid: str) -> dict:
    """
    Convert an Acorn Sprite file to PNG images using the spritefile library.

    Args:
        input_path: Path to the .spr sprite file
        output_dir: Directory to write output PNGs into
        analysis_uuid: UUID string used to prefix output filenames

    Returns:
        Dict with keys:
            success (bool)
            sprites (list of {'name': str, 'path': Path})
            error (str | None)
    """
    try:
        import spritefile
        from PIL import Image
    except ImportError as e:
        return tool_result(
            False, tool='spritefile', error=f'Missing dependency: {e}', sprites=[],
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    sprites = []
    error = None

    try:
        raw = input_path.read_bytes()
    except OSError as e:
        return tool_result(False, tool='spritefile', error=f'Cannot read file: {e}', sprites=[])

    try:
        with io.BytesIO(raw) as fh:
            sf = spritefile.spritefile(file=fh)
        lib_warnings = list(getattr(sf, 'warnings', []))
        for w in lib_warnings:
            log.warning('spritefile warning in %s: %s', input_path.name, w)
        sprite_list = list(sf.sprites.items())  # [(name, sprite_dict), ...]
    except Exception as e:
        log.warning('Failed to open sprite file: %s\n%s', e, _tb.format_exc().rstrip())
        return tool_result(
            False, tool='spritefile',
            error=f'Failed to open sprite file: {e}', sprites=[], warnings=[],
        )

    if not sprite_list:
        return tool_result(
            False, tool='spritefile', error='No sprites found in file', sprites=[], warnings=lib_warnings,
        )

    for idx, (name, sprite) in enumerate(sprite_list):
        try:
            name = name or f'sprite{idx:02d}'
            safe_name = _safe_sprite_name(name, idx)
            out_filename = f'{analysis_uuid}_{idx:02d}_{safe_name}.png'
            out_path = output_dir / out_filename

            img = Image.frombytes(
                sprite['mode'],
                (sprite['width'], sprite['height']),
                bytes(sprite['image']),
            )
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA')

            # Correct non-square pixels (e.g. Mode 13 / Mode 15: xdpi=90, ydpi=45).
            # Physical pixel size = 1/dpi inches; when X and Y DPI differ the pixels
            # are rectangular.  Scale the image so pixels become square on screen.
            xdpi = sprite.get('dpi x', 0)
            ydpi = sprite.get('dpi y', 0)
            if xdpi > 0 and ydpi > 0 and xdpi != ydpi:
                if xdpi > ydpi:
                    # Y pixels are taller than wide — stretch height.
                    new_h = round(img.height * xdpi / ydpi)
                    img = img.resize((img.width, new_h), Image.NEAREST)
                else:
                    # X pixels are wider than tall — stretch width.
                    new_w = round(img.width * ydpi / xdpi)
                    img = img.resize((new_w, img.height), Image.NEAREST)

            img.save(str(out_path), 'PNG')

            sprites.append({'name': name, 'path': out_path})
            log.debug(f'Saved sprite "{name}" → {out_path}')
        except Exception as e:
            log.warning(f'Failed to convert sprite {idx} ("{name}"): {e}')
            error = str(e)

    success = len(sprites) > 0
    return tool_result(
        success,
        tool='spritefile',
        error=(error if not success else None),
        sprites=sprites,
        warnings=lib_warnings,
    )


def convert_draw(input_path: Path, output_dir: Path, analysis_uuid: str) -> dict:
    """
    Convert an Acorn Draw file to SVG using drawfile_render.

    The tool is installed as a plain script at /opt/drawfile_render/render_drawfile.py
    (not a pip package).  SVG is produced by importing DrawFileRender directly.
    PYTHONPATH includes /opt/drawfile_render so the import works in Docker.

    Args:
        input_path: Path to the .aff/.draw file
        output_dir: Directory to write output files into
        analysis_uuid: UUID string used to prefix output filenames

    Returns:
        Dict with keys:
            success (bool)
            svg_path (Path | None)
            error (str | None)
            process_output (dict)
    """
    _DRAWFILE_RENDER_DIR = '/opt/drawfile_render'

    output_dir.mkdir(parents=True, exist_ok=True)
    output_base = str(output_dir / f'{analysis_uuid}_draw')
    svg_candidate = Path(output_base + '.svg')
    process_output: dict = {}

    try:
        import sys as _sys
        if _DRAWFILE_RENDER_DIR not in _sys.path:
            _sys.path.insert(0, _DRAWFILE_RENDER_DIR)
        from render_drawfile import DrawFileRender
        df = DrawFileRender(filename=str(input_path))
        df.render_to_context(filename=output_base, img_format='svg')
    except Exception as e:
        log.warning(f'SVG generation failed for {input_path}: {e}')
        return tool_result(
            False, tool='drawfile_render', error=str(e),
            process_output=process_output, svg_path=None,
        )

    if not svg_candidate.exists():
        log.warning(f'drawfile_render produced no SVG output for {input_path}')
        return tool_result(
            False, tool='drawfile_render',
            error='drawfile_render produced no output',
            process_output=process_output, svg_path=None,
        )

    return tool_result(
        True, tool='drawfile_render',
        process_output=process_output, svg_path=svg_candidate,
    )

# vim: ts=4 sw=4 et
