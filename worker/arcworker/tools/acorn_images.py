"""
Acorn/RISC OS image conversion tools.

Provides wrappers for converting Acorn Sprite and Draw files to portable formats.
"""

import re
from pathlib import Path

from ..config import log
from .base import run_tool_with_output
from ..utils.text import sanitize_filename


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
        return {'success': False, 'sprites': [], 'error': f'Missing dependency: {e}'}

    output_dir.mkdir(parents=True, exist_ok=True)
    sprites = []
    error = None

    try:
        with open(str(input_path), 'rb') as fh:
            sf = spritefile.spritefile(file=fh)
        sprite_list = list(sf.sprites.items())  # [(name, sprite_dict), ...]
    except Exception as e:
        return {'success': False, 'sprites': [], 'error': f'Failed to open sprite file: {e}'}

    if not sprite_list:
        return {'success': False, 'sprites': [], 'error': 'No sprites found in file'}

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
            img.save(str(out_path), 'PNG')

            sprites.append({'name': name, 'path': out_path})
            log.debug(f'Saved sprite "{name}" → {out_path}')
        except Exception as e:
            log.warning(f'Failed to convert sprite {idx} ("{name}"): {e}')
            error = str(e)

    success = len(sprites) > 0
    return {'success': success, 'sprites': sprites, 'error': error if not success else None}


def convert_draw(input_path: Path, output_dir: Path, analysis_uuid: str,
                 generate_svg: bool = False) -> dict:
    """
    Convert an Acorn Draw file to PNG (and optionally SVG) using drawfile_render.

    The tool is installed as a plain script at /opt/drawfile_render/render_drawfile.py
    (not a pip package).  PNG is produced by invoking it as a subprocess; SVG is
    produced by importing DrawFileRender directly (the CLI does not expose --format).

    Args:
        input_path: Path to the .aff/.draw file
        output_dir: Directory to write output files into
        analysis_uuid: UUID string used to prefix output filenames
        generate_svg: Also produce an SVG alongside the PNG (default False)

    Returns:
        Dict with keys:
            success (bool)
            png_path (Path | None)
            svg_path (Path | None)
            error (str | None)
            tool_output (dict)
    """
    _DRAWFILE_RENDER_DIR = '/opt/drawfile_render'
    _DRAWFILE_RENDER_SCRIPT = f'{_DRAWFILE_RENDER_DIR}/render_drawfile.py'

    output_dir.mkdir(parents=True, exist_ok=True)
    # render_drawfile.py appends .png to the --output value automatically
    output_base = str(output_dir / f'{analysis_uuid}_draw')
    png_path = Path(output_base + '.png')

    result, tool_output = run_tool_with_output(
        ['python3', _DRAWFILE_RENDER_SCRIPT, '--input', str(input_path), '--output', output_base],
        cwd=_DRAWFILE_RENDER_DIR,
    )

    if result.returncode != 0 or not png_path.exists():
        error_msg = tool_output.get('stderr', '') or tool_output.get('stdout', '') or 'drawfile_render failed'
        return {
            'success': False,
            'png_path': None,
            'svg_path': None,
            'error': error_msg[:500],
            'tool_output': tool_output,
        }

    # Optionally produce SVG by importing DrawFileRender directly.
    # The CLI hardcodes PNG; SVG is available via the Python API.
    # PYTHONPATH includes /opt/drawfile_render so the import works in Docker.
    svg_path = None
    if generate_svg:
        svg_candidate = Path(output_base + '.svg')
        try:
            import sys as _sys
            if _DRAWFILE_RENDER_DIR not in _sys.path:
                _sys.path.insert(0, _DRAWFILE_RENDER_DIR)
            from render_drawfile import DrawFileRender
            df = DrawFileRender(filename=str(input_path))
            df.render_to_context(filename=output_base, img_format='svg')
            if svg_candidate.exists():
                svg_path = svg_candidate
            else:
                log.warning(f'SVG render produced no output for {input_path}')
        except Exception as e:
            log.warning(f'SVG generation failed for {input_path}: {e}')

    return {
        'success': True,
        'png_path': png_path,
        'svg_path': svg_path,
        'error': None,
        'tool_output': tool_output,
    }

# vim: ts=4 sw=4 et
