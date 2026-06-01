"""
Common image format conversion tools.

Handles raster images (JPEG, PNG, GIF, BMP, TIFF, WebP, PCX, TGA) via Pillow
and Windows vector metafiles (WMF, EMF) via external tools (wmf2svg, Dexvert emf2svg).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from ..config import log
from .base import run_tool_with_output
from .svg_utils import postprocess_svg

# Extensions passed through unchanged (browser-native formats)
_PASSTHROUGH_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.webp'})


def _sniff_browser_native_ext(input_path: Path) -> str | None:
    """Identify a browser-native image by its magic bytes.

    Returns the canonical extension ('.jpg', '.png', '.gif', '.webp') if the
    file's leading bytes match a browser-native raster format, else None.

    This backstops the extension-based passthrough check: RISC OS extracts
    name files with a ',xxx' filetype suffix (e.g. 'Photo,c85') rather than a
    DOS extension, so a real JPEG arrives with no '.jpg' suffix and would
    otherwise be needlessly re-encoded to PNG by the Pillow fallback.
    """
    try:
        with open(input_path, 'rb') as f:
            header = f.read(12)
    except OSError:
        return None
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return '.webp'
    return None

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
    """Build an image-conversion result dict.

    Always emits the ``output_path``, ``format`` and ``error`` keys
    (including when they are ``None``) so downstream consumers can rely
    on their presence.
    """
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
                proc, _ = run_tool_with_output(cmd, timeout=60)
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
    # Trust the extension first; for files without a recognised extension
    # (notably RISC OS extracts named 'Photo,c85' with no '.jpg' suffix),
    # fall back to a magic-byte sniff so browser-native formats are copied
    # through unchanged rather than re-encoded to PNG below.
    passthrough_ext = ext if ext in _PASSTHROUGH_EXTS else _sniff_browser_native_ext(input_path)
    if passthrough_ext:
        out_path = output_dir / f'{analysis_uuid}_image{passthrough_ext}'
        shutil.copy2(input_path, out_path)
        return _result(
            True,
            output_path=str(out_path),
            fmt=passthrough_ext.lstrip('.').upper(),
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

    # Redirect C-level stderr (fd 2) so that LibTIFF error messages printed
    # directly by libtiff before the OSError is raised can be captured and
    # included in the failure record.
    _stderr_tmp = tempfile.TemporaryFile()
    _stderr_saved = os.dup(2)
    os.dup2(_stderr_tmp.fileno(), 2)
    _exc: Exception | None = None
    _fmt: str | None = None
    try:
        with Image.open(input_path) as img:
            _fmt = img.format or ext.lstrip('.').upper()
            if img.mode not in ('RGB', 'RGBA', 'L', 'P'):
                img = img.convert('RGBA')
            img.save(str(out_path), 'PNG')
    except (OSError, UnidentifiedImageError) as e:
        _exc = e
    finally:
        # Restore fd 2 before any logging so worker output is not swallowed.
        sys.stderr.flush()
        os.dup2(_stderr_saved, 2)
        os.close(_stderr_saved)
        _stderr_tmp.seek(0)
        _libtiff_detail = _stderr_tmp.read().decode('utf-8', errors='replace').strip()
        _stderr_tmp.close()

    if _exc is None:
        return _result(
            True,
            output_path=str(out_path),
            fmt=_fmt,
            tool='pillow-convert',
            error=None,
        )

    if isinstance(_exc, UnidentifiedImageError):
        log.warning('Image conversion failed for %s (unidentified format): %s',
                    input_path, _exc)
        return _result(
            False,
            output_path=None,
            fmt=None,
            tool='pillow-convert',
            error=str(_exc),
        )

    # Plain OSError — Ubuntu's Pillow raises OSError(negative_int) when the
    # C codec fails (e.g. OSError(-2) for a malformed TIFF), giving str(e)=="-2".
    # Translate via PIL.ImageFile.ERRORS and append any captured LibTIFF detail.
    msg = str(_exc)
    if _exc.args and isinstance(_exc.args[0], int) and _exc.args[0] < 0:
        code = _exc.args[0]
        try:
            from PIL.ImageFile import ERRORS as _PIL_ERRORS
            description = _PIL_ERRORS.get(code, 'unknown codec error')
        except ImportError:
            description = 'unknown codec error'
        msg = f'Pillow codec error {code}: {description}'
        if _libtiff_detail:
            msg += f' — {_libtiff_detail}'
    log.warning('Image conversion failed for %s: %s', input_path, msg)
    return _result(
        False,
        output_path=None,
        fmt=None,
        tool='pillow-convert',
        error=msg,
    )

# vim: ts=4 sw=4 et
