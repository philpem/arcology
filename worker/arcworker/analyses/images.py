"""
Format-conversion handler.

Renders RISC OS Sprite, DrawFile and Text artefacts, documents (MS Word, PDF,
…) and bitmap images into web-viewable outputs.  Two operating modes:

  Mode 1 — Direct artefact (artefact_type is ACORN_SPRITE/DRAW/TEXT, IMAGE, or a
           document-text type in _DOCUMENT_TEXT_CONVERTERS): convert the
           artefact's own file.
  Mode 2 — Extraction scan (hints contain ``extraction_path``): walk the
           extraction output directory and convert every viewable file
           found.
"""

import json
import signal
from contextlib import contextmanager
from pathlib import Path
from arcology_shared.artefact_types import (
    RISCOS_VIEWABLE_SUFFIXES,
    VIEWABLE_EXTENSIONS,
    viewable_artefact_type,
)
from arcology_shared.enums import AnalysisType, ArtefactType
from ..config import log
from ..tools import (
    convert_draw,
    convert_sprite,
    parse_acorn_filename,
    pdf_to_text,
    read_file_capped,
    word_to_text,
)
from ..utils.paths import artefact_output_subdir
from ._common import analysis_handler, iter_resolved_files, scan_partition_files

# Per-file timeout (seconds) for pure-Python conversion calls (spritefile,
# DrawFileRender, PIL).  These libraries have no internal timeout; a
# malformed input can cause a library to spin at 100 % CPU indefinitely.
# SIGALRM is Unix-only but the worker always runs on Linux in Docker.
_PER_FILE_CONVERT_TIMEOUT = 120

# Cap on the converted text carried in FORMAT_CONVERT details for full-text
# indexing.  Retro text files are tiny, but this bounds the details payload (and
# the search_documents row) for a pathological input.  Truncation is flagged so
# the indexer / UI can note it rather than silently losing content.
_INDEX_TEXT_CAP = 200_000

# Artefact types converted to plain text via an external/stdlib converter that
# returns a ``tool_result`` with a ``text`` field.  All are dispatched by one
# branch in _convert_file_to_outputs_inner and routed through _text_output(), so
# adding a text format is one entry here plus its detection wiring.
_DOCUMENT_TEXT_CONVERTERS = {
    ArtefactType.MS_WORD: word_to_text,
    ArtefactType.PDF: pdf_to_text,
}


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


def _text_output(self, text: str, *, name: str, work_dir: Path,
                 output_subdir: str | None, analysis_uuid: str,
                 file_index: int, tool: str, slug: str = 'text') -> dict:
    """Write `text` as a UTF-8 ``.txt`` output and build its FORMAT_CONVERT dict.

    The single home for the text-output contract shared by every text-producing
    converter (ACORN_TEXT today, MS Word and the DTP formats next): the saved
    ``.txt`` holds the full text (viewable in the browser), while the returned
    dict carries a capped copy (`text` + `text_truncated`) for full-text
    indexing into ``search_documents``.  Routing a new format's plain text
    through here makes it searchable *and* viewable without re-deriving the cap.
    """
    out_filename = f'{analysis_uuid}_{file_index}_{slug}.txt'
    out_path = work_dir / out_filename
    out_path.write_text(text, encoding='utf-8')
    saved = self.save_output_file(out_path, out_filename, subdir=output_subdir)
    return {
        'type': 'text',
        'filename': saved,
        'name': name,
        'description': name,
        'tool': tool,
        # Capped copy of the converted text for full-text indexing
        # (search_documents).  The full text remains in the saved output.
        'text': text[:_INDEX_TEXT_CAP],
        'text_truncated': len(text) > _INDEX_TEXT_CAP,
    }

# Viewable-file lookup tables now live in arcology_shared (the single source the
# content classifier also reads); aliased here under their original names so the
# worker-class attribute bindings (AnalysisWorker._RISCOS_VIEWABLE_SUFFIXES, …)
# and this module's exports keep working unchanged.
_RISCOS_VIEWABLE_SUFFIXES = RISCOS_VIEWABLE_SUFFIXES
_EXT_VIEWABLE = VIEWABLE_EXTENSIONS


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
            raw = read_file_capped(input_path)
            # Decode as Latin-1 (covers all Acorn/DOS byte values);
            # normalise RISC OS line endings (0x0A) to LF.
            text = raw.decode('latin-1').replace('\r\n', '\n').replace('\r', '\n')
            outputs.append(_text_output(
                self, text, name=true_name, work_dir=work_dir,
                output_subdir=output_subdir, analysis_uuid=analysis_uuid,
                file_index=file_index, tool='builtin',
            ))
        except Exception as e:
            log.warning(f"Text conversion failed for {input_path}: {e}")
            return None, str(e), warnings

    elif artefact_type in _DOCUMENT_TEXT_CONVERTERS:
        true_name, _ = parse_acorn_filename(input_path.name)
        converter = _DOCUMENT_TEXT_CONVERTERS[artefact_type]
        with _conversion_timeout(_PER_FILE_CONVERT_TIMEOUT, input_path.name):
            result = converter(input_path)
        warnings.extend(result.get('warnings', []))
        if not result['success']:
            log.warning(f"{artefact_type.value} conversion failed for {input_path}: {result.get('error')}")
            return None, result.get('error') or 'Conversion failed', warnings
        # Converters emit UTF-8; normalise line endings.  Skip a blank result
        # (e.g. an image-only scanned PDF) rather than emit an empty .txt.
        text = (result.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
        if text.strip():
            outputs.append(_text_output(
                self, text, name=true_name, work_dir=work_dir,
                output_subdir=output_subdir, analysis_uuid=analysis_uuid,
                file_index=file_index, tool=result.get('tool', artefact_type.value),
            ))

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

    Mode 1 — Direct artefact (artefact_type is ACORN_SPRITE/DRAW/TEXT, IMAGE, or
      a document-text type in _DOCUMENT_TEXT_CONVERTERS): Convert the artefact's
      own file.  Used for directly-uploaded Acorn files and documents; triggered
      via ANALYSIS_MAP.

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

    output_subdir = artefact_output_subdir(artefact)

    _direct_types = (
        ArtefactType.ACORN_SPRITE.value,
        ArtefactType.ACORN_DRAW.value,
        ArtefactType.ACORN_TEXT.value,
        ArtefactType.IMAGE.value,
        *(t.value for t in _DOCUMENT_TEXT_CONVERTERS),
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
    # Determine viewable type from DB metadata (filetype hex, then extension) —
    # the shared classifier's predicate, so file selection here cannot drift from
    # classify_content's CONVERTIBLE category.  Returns None for non-viewable files.
    def _viewable_type_from_db(file_data: dict) -> 'ArtefactType | None':
        return viewable_artefact_type(
            file_data.get('filename', ''), file_data.get('risc_os_filetype'))

    # Discover viewable files via the shared batch scaffold.  The select
    # predicate annotates each selected DB record with its viewable type, which
    # the conversion loop reads back off file_data rather than re-deriving.
    def _select(file_data: dict) -> bool:
        vt = _viewable_type_from_db(file_data)
        if vt is not None:
            file_data['_viewable_type'] = vt
            return True
        return False

    scan = scan_partition_files(self, analysis, artefact, select_files=_select)
    if scan is None:
        self.fail_analysis(
            analysis_id,
            f'FORMAT_CONVERT not supported for artefact type {artefact_type_str!r} '
            f'and no extraction_path hint provided',
        )
        return

    all_outputs = []
    failed_conversions = []
    file_index = 0

    def _missing(file_data, db_path):
        log.warning(f"Viewable file not found: {db_path}")

    for file_data, file_path, db_path in iter_resolved_files(
            self, scan.files, scan.extraction_path, work_dir,
            path_prefix=scan.path_prefix, on_missing=_missing):
        viewable_type = file_data.get('_viewable_type') or _viewable_type_from_db(file_data)
        if viewable_type is None:
            log.warning(f"Skipping {db_path} — no viewable type in metadata")
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
