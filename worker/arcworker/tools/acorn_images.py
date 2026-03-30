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
        sf = spritefile.SpriteFile(str(input_path))
        sprite_list = list(sf)
    except Exception as e:
        return {'success': False, 'sprites': [], 'error': f'Failed to open sprite file: {e}'}

    if not sprite_list:
        return {'success': False, 'sprites': [], 'error': 'No sprites found in file'}

    for idx, sprite in enumerate(sprite_list):
        try:
            name = getattr(sprite, 'name', '') or f'sprite{idx:02d}'
            safe_name = _safe_sprite_name(name, idx)
            out_filename = f'{analysis_uuid}_{idx:02d}_{safe_name}.png'
            out_path = output_dir / out_filename

            img = sprite.image
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


def convert_draw(input_path: Path, output_dir: Path, analysis_uuid: str) -> dict:
    """
    Convert an Acorn Draw file to PNG (and optionally SVG) using drawfile_render.

    Args:
        input_path: Path to the .aff/.draw file
        output_dir: Directory to write output files into
        analysis_uuid: UUID string used to prefix output filenames

    Returns:
        Dict with keys:
            success (bool)
            png_path (Path | None)
            svg_path (Path | None)
            error (str | None)
            tool_output (dict)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / f'{analysis_uuid}_draw.png'
    svg_path = output_dir / f'{analysis_uuid}_draw.svg'

    # Try PNG output first
    result_png, tool_output_png = run_tool_with_output([
        'python3', '-m', 'drawfile_render',
        '--output', str(png_path),
        str(input_path),
    ])

    if result_png.returncode == 0 and png_path.exists():
        # Optionally attempt SVG
        result_svg, _tool_output_svg = run_tool_with_output([
            'python3', '-m', 'drawfile_render',
            '--format', 'svg',
            '--output', str(svg_path),
            str(input_path),
        ])
        actual_svg = svg_path if (result_svg.returncode == 0 and svg_path.exists()) else None

        return {
            'success': True,
            'png_path': png_path,
            'svg_path': actual_svg,
            'error': None,
            'tool_output': tool_output_png,
        }

    error_msg = tool_output_png.get('stderr', '') or tool_output_png.get('stdout', '') or 'drawfile_render failed'
    return {
        'success': False,
        'png_path': None,
        'svg_path': None,
        'error': error_msg[:500],
        'tool_output': tool_output_png,
    }

# vim: ts=4 sw=4 et
