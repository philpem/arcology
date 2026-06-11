"""
Format-conversion handler.

Renders RISC OS Sprite, DrawFile and Text artefacts (and bitmap images)
into web-viewable outputs.  Two operating modes:

  Mode 1 — Direct artefact (artefact_type ∈ {ACORN_SPRITE, ACORN_DRAW,
           ACORN_TEXT, IMAGE}): convert the artefact's own file.
  Mode 2 — Extraction scan (hints contain ``extraction_path``): walk the
           extraction output directory and convert every viewable file
           found.
"""

import json
import signal
from contextlib import contextmanager
from pathlib import Path
from shared.enums import AnalysisType, ArtefactType
from ..config import log
from ..tools import (
    convert_draw,
    convert_sprite,
    parse_acorn_filename,
)
from ..utils.paths import artefact_output_subdir
from ._common import analysis_handler

# Per-file timeout (seconds) for pure-Python conversion calls (spritefile,
# DrawFileRender, PIL).  These libraries have no internal timeout; a
# malformed input can cause a library to spin at 100 % CPU indefinitely.
# SIGALRM is Unix-only but the worker always runs on Linux in Docker.
_PER_FILE_CONVERT_TIMEOUT = 120


@contextmanager
def _conversion_timeout(seconds: int, label: str = ''):
    """Raise TimeoutError if the block does not complete within `seconds`."""
    def _handler(signum, frame):
        raise TimeoutError(
            f"Conversion timed out after {seconds}s"
            + (f' ({label})' if label else '')
        )
    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

# RISC OS filetype suffixes that indicate viewable file types.
# Mapping: suffix (e.g. ',ff9') → ArtefactType
_RISCOS_VIEWABLE_SUFFIXES: dict[str, 'ArtefactType'] = {
    ',ff9': ArtefactType.ACORN_SPRITE,  # Sprite
    ',aff': ArtefactType.ACORN_DRAW,    # DrawFile
    ',fff': ArtefactType.ACORN_TEXT,    # Text
    ',feb': ArtefactType.ACORN_TEXT,    # Obey
    ',ffe': ArtefactType.ACORN_TEXT,    # Command
    ',c85': ArtefactType.IMAGE,         # JPEG
    ',695': ArtefactType.IMAGE,         # GIF
    ',b60': ArtefactType.IMAGE,         # PNG
    ',69c': ArtefactType.IMAGE,         # BMP
    ',ff0': ArtefactType.IMAGE,         # TIFF
}
# Extension-based detection (used for DOS discs without RISC OS metadata)
_EXT_VIEWABLE: dict[str, 'ArtefactType'] = {
    '.spr':  ArtefactType.ACORN_SPRITE,
    '.aff':  ArtefactType.ACORN_DRAW,
    '.draw': ArtefactType.ACORN_DRAW,
    '.txt':  ArtefactType.ACORN_TEXT,
    '.jpg':  ArtefactType.IMAGE,
    '.jpeg': ArtefactType.IMAGE,
    '.png':  ArtefactType.IMAGE,
    '.gif':  ArtefactType.IMAGE,
    '.webp': ArtefactType.IMAGE,
    '.bmp':  ArtefactType.IMAGE,
    '.tif':  ArtefactType.IMAGE,
    '.tiff': ArtefactType.IMAGE,
    '.pcx':  ArtefactType.IMAGE,
    '.tga':  ArtefactType.IMAGE,
    '.wmf':  ArtefactType.IMAGE,
    '.emf':  ArtefactType.IMAGE,
}
# RISC OS filetype hex → viewable type, for ISO files where no ',xxx'
# suffix is present on the extracted filename but risc_os_filetype is
# available from the ARCHIMEDES extension metadata sidecar.
# Note: &D94 (ArtWorks), &D87/&D88 (Impression), &D01 (TechWriter) are
# intentionally omitted — they require bespoke rendering tools.
_RISCOS_HEX_VIEWABLE: dict[str, 'ArtefactType'] = {
    'ff9': ArtefactType.ACORN_SPRITE,
    'aff': ArtefactType.ACORN_DRAW,
    'fff': ArtefactType.ACORN_TEXT,
    'feb': ArtefactType.ACORN_TEXT,
    'ffe': ArtefactType.ACORN_TEXT,
    'c85': ArtefactType.IMAGE,  # JPEG
    '695': ArtefactType.IMAGE,  # GIF
    'b60': ArtefactType.IMAGE,  # PNG
    '69c': ArtefactType.IMAGE,  # BMP
    'ff0': ArtefactType.IMAGE,  # TIFF
}


def _convert_file_to_outputs(
    self,
    input_path: Path,
    artefact_type: 'ArtefactType',
    work_dir: Path,
    output_subdir: str | None,
    analysis_uuid: str,
    file_index: int = 0,
) -> tuple[list[dict] | None, str | None, list[str]]:
    """
    Convert a single viewable file and return ``(outputs, error, warnings)``.

    On success: ``(list_of_output_dicts, None, warnings)``.
    On failure: ``(None, error_message, warnings)`` — caller should call
    ``fail_analysis`` (Mode 1) or record the failure and continue (Mode 2).
    ``warnings`` collects non-fatal conversion warnings in both cases.

    ``file_index`` is used to make temporary subdirectory names unique when
    converting multiple files within one analysis run.
    """
    try:
        return _convert_file_to_outputs_inner(
            self, input_path, artefact_type, work_dir, output_subdir,
            analysis_uuid, file_index,
        )
    except TimeoutError as exc:
        log.warning("Conversion timed out for %s (%s): %s", input_path, artefact_type.value, exc)
        return None, str(exc), []


def _convert_file_to_outputs_inner(
    self,
    input_path: Path,
    artefact_type: 'ArtefactType',
    work_dir: Path,
    output_subdir: str | None,
    analysis_uuid: str,
    file_index: int = 0,
) -> tuple[list[dict] | None, str | None, list[str]]:
    outputs = []
    warnings: list[str] = []

    if artefact_type == ArtefactType.ACORN_SPRITE:
        tmp_out = work_dir / f'sprites_{file_index}'
        with _conversion_timeout(_PER_FILE_CONVERT_TIMEOUT, input_path.name):
            result = convert_sprite(input_path, tmp_out, analysis_uuid)
        warnings.extend(result.get('warnings', []))
        if not result['success']:
            log.warning(f"Sprite conversion failed for {input_path}: {result.get('error')}")
            return None, result.get('error') or 'Conversion failed', warnings
        for sprite in result['sprites']:
            # Include file_index in the saved name so that sprites from
            # different source files within the same analysis run don't
            # overwrite each other.  sprite['path'].name is already
            # f'{analysis_uuid}_{idx:02d}_{safe_name}.png'; insert
            # file_index after the uuid prefix.
            orig_stem = sprite['path'].stem  # '{uuid}_{idx}_{name}'
            rest = orig_stem[len(analysis_uuid) + 1:]  # '{idx}_{name}'
            unique_name = f'{analysis_uuid}_{file_index}_{rest}.png'
            saved = self.save_output_file(
                sprite['path'],
                unique_name,
                subdir=output_subdir,
            )
            outputs.append({
                'type': 'image',
                'filename': saved,
                'name': sprite['name'],
                'description': sprite['name'],
                'tool': 'spritefile',
            })

    elif artefact_type == ArtefactType.ACORN_DRAW:
        true_name, _ = parse_acorn_filename(input_path.name)
        tmp_out = work_dir / f'draw_{file_index}'
        with _conversion_timeout(_PER_FILE_CONVERT_TIMEOUT, input_path.name):
            result = convert_draw(input_path, tmp_out, analysis_uuid)
        if not result['success']:
            return None, result.get('error') or 'Conversion failed', warnings
        # Include file_index so multiple Draw files in the same archive
        # each get a unique output filename rather than overwriting each other.
        saved_svg = self.save_output_file(
            result['svg_path'],
            f'{analysis_uuid}_{file_index}_draw.svg',
            subdir=output_subdir,
        )
        outputs.append({
            'type': 'image',
            'filename': saved_svg,
            'name': true_name,
            'description': true_name,
            'tool': 'drawfile_render',
        })

    elif artefact_type == ArtefactType.ACORN_TEXT:
        true_name, _ = parse_acorn_filename(input_path.name)
        try:
            raw = input_path.read_bytes()
            # Decode as Latin-1 (covers all Acorn/DOS byte values);
            # normalise RISC OS line endings (0x0A) to LF.
            text = raw.decode('latin-1').replace('\r\n', '\n').replace('\r', '\n')
            out_filename = f'{analysis_uuid}_{file_index}_text.txt'
            out_path = work_dir / out_filename
            out_path.write_text(text, encoding='utf-8')
            saved = self.save_output_file(out_path, out_filename, subdir=output_subdir)
            outputs.append({
                'type': 'text',
                'filename': saved,
                'name': true_name,
                'description': true_name,
                'tool': 'builtin',
            })
        except Exception as e:
            log.warning(f"Text conversion failed for {input_path}: {e}")
            return None, str(e), warnings

    elif artefact_type == ArtefactType.IMAGE:
        from ..tools.images_common import convert_image  # numpy/scour: worker-only
        true_name, _ = parse_acorn_filename(input_path.name)
        tmp_out = work_dir / f'image_{file_index}'
        with _conversion_timeout(_PER_FILE_CONVERT_TIMEOUT, input_path.name):
            result = convert_image(input_path, tmp_out, analysis_uuid)
        if not result['success']:
            log.warning(f"Image conversion failed for {input_path}: {result.get('error')}")
            return None, result.get('error') or 'Conversion failed', warnings
        ext = Path(result['output_path']).suffix
        saved = self.save_output_file(
            Path(result['output_path']),
            f'{analysis_uuid}_{file_index}_image{ext}',
            subdir=output_subdir,
        )
        outputs.append({
            'type': 'image',
            'filename': saved,
            'name': true_name,
            'description': true_name,
            'tool': result['tool'],
        })

    return outputs, None, warnings


def _detect_viewable_type(self, path: Path) -> 'ArtefactType | None':
    """Return the ArtefactType for a viewable file, or None if not viewable."""
    name_lower = path.name.lower()
    for suffix, atype in self._RISCOS_VIEWABLE_SUFFIXES.items():
        if name_lower.endswith(suffix):
            return atype
    return self._EXT_VIEWABLE.get(path.suffix.lower())


@analysis_handler("format conversion", AnalysisType.FORMAT_CONVERT)
def process_format_convert(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process FORMAT_CONVERT analysis.  Supports two modes:

    Mode 1 — Direct artefact (artefact_type is ACORN_SPRITE/DRAW/TEXT/IMAGE):
      Convert the artefact's own file.  Used for directly-uploaded Acorn
      files; triggered via ANALYSIS_MAP.

    Mode 2 — Extraction scan (hints contain 'extraction_path'):
      Scan the extraction output directory for every viewable file, convert
      each one, and store outputs with a 'source_file' field matching
      ExtractedFile.path (display path, Acorn filetype suffix stripped).
      Queued automatically by queue_partition_follow_ups() after every
      FILE_EXTRACTION and ARCHIVE_EXTRACT.
    """
    analysis_id = analysis['id']
    analysis_uuid = analysis['uuid']
    artefact_type_str = artefact.get('artefact_type', '')
    hints = json.loads(analysis.get('hints') or '{}')

    output_subdir = artefact_output_subdir(artefact)

    _direct_types = (
        ArtefactType.ACORN_SPRITE.value,
        ArtefactType.ACORN_DRAW.value,
        ArtefactType.ACORN_TEXT.value,
        ArtefactType.IMAGE.value,
    )

    # --- Mode 1: Direct artefact conversion ---
    if artefact_type_str in _direct_types:
        input_path = self.get_input_path(artefact, work_dir)
        artefact_type = ArtefactType(artefact_type_str)
        outputs, error, file_warnings = self._convert_file_to_outputs(
            input_path, artefact_type, work_dir, output_subdir, analysis_uuid,
        )
        if outputs is None:
            self.fail_analysis(
                analysis_id,
                f'Conversion failed for {artefact_type_str}: {error or "unknown error"}',
            )
            return
        self.complete_analysis(
            analysis_id,
            summary=f'Converted {len(outputs)} output(s) for {artefact_type_str}',
            details=json.dumps({
                'artefact_type': artefact_type_str,
                'outputs': outputs,
                'warnings': file_warnings,
            }),
        )
        return

    # --- Mode 2: Extraction scan ---
    extraction_path = hints.get('extraction_path')
    partition_uuid = hints.get('partition_uuid')
    path_prefix = hints.get('path_prefix', '')  # e.g. 'Archives/Emulators.zip'
    if not extraction_path:
        self.fail_analysis(
            analysis_id,
            f'FORMAT_CONVERT not supported for artefact type {artefact_type_str!r} '
            f'and no extraction_path hint provided',
        )
        return

    # Determine viewable type from DB metadata.  Returns None for
    # files that are not viewable (not a sprite, draw, or text file).
    def _viewable_type_from_db(file_data: dict) -> 'ArtefactType | None':
        ft = (file_data.get('risc_os_filetype') or '').lower()
        if ft:
            vt = self._RISCOS_HEX_VIEWABLE.get(ft)
            if vt:
                return vt
        filename = file_data.get('filename', '')
        ext = Path(filename).suffix.lower()
        return self._EXT_VIEWABLE.get(ext)

    # Query file list from the database via API instead of scanning the
    # filesystem.  This avoids downloading the entire extraction tree
    # in S3 mode — only the viewable files will be fetched individually.
    # Push the extraction-context filter to the API.
    viewable_files: list[tuple[dict, ArtefactType]] = []
    if partition_uuid:
        base_params = {'show_known': 'true'}
        if path_prefix:
            base_params['path_prefix'] = path_prefix
        else:
            base_params['extraction_depth'] = 0

        all_files = self.api.get_partition_files(partition_uuid, **base_params)

        for file_data in all_files:
            if file_data.get('is_directory', False):
                continue
            vt = _viewable_type_from_db(file_data)
            if vt is not None:
                viewable_files.append((file_data, vt))

    all_outputs = []
    failed_conversions = []
    file_index = 0
    for file_data, viewable_type in viewable_files:
        db_path = file_data['path']

        # Strip the archive path prefix to get the on-disk relative path
        if path_prefix and db_path.startswith(path_prefix + '/'):
            disk_path = db_path[len(path_prefix) + 1:]
        else:
            disk_path = db_path

        file_path = self._resolve_single_extraction_file(
            extraction_path, disk_path, work_dir,
            risc_os_filetype=file_data.get('risc_os_filetype') or None,
        )
        if file_path is None and disk_path != db_path:
            # Fallback: the archive may contain a top-level directory
            # whose name matches the archive filename (common in RISC OS).
            # In that case the on-disk path retains the prefix, so try
            # the full DB path without stripping.
            file_path = self._resolve_single_extraction_file(
                extraction_path, db_path, work_dir,
                risc_os_filetype=file_data.get('risc_os_filetype') or None,
            )
        if file_path is None:
            log.warning(f"Viewable file not found: {db_path}")
            continue

        # display_path is the DB path (already matches ExtractedFile.path)
        display_path = db_path

        file_outputs, file_error, file_warnings = self._convert_file_to_outputs(
            file_path, viewable_type, work_dir, output_subdir, analysis_uuid, file_index,
        )
        file_index += 1
        if file_outputs is None:
            log.warning(f"Skipping {file_path} — conversion failed: {file_error}")
            failed_conversions.append({
                'source_file': display_path,
                'error': file_error or 'Conversion failed',
                'warnings': file_warnings,
            })
            continue
        for out in file_outputs:
            out['source_file'] = display_path
            if file_warnings:
                out['warnings'] = file_warnings
        all_outputs.extend(file_outputs)

    failed_suffix = f' ({len(failed_conversions)} failed)' if failed_conversions else ''
    self.complete_analysis(
        analysis_id,
        summary=f'Converted {len(all_outputs)} output(s) from {file_index} viewable file(s){failed_suffix}',
        details=json.dumps({
            'mode': 'extraction_scan',
            'outputs': all_outputs,
            'failed_conversions': failed_conversions,
        }),
    )
# vim: ts=4 sw=4 et
