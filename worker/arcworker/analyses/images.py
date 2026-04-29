"""
Format-conversion handler.

Renders RISC OS Sprite, DrawFile and Text artefacts (and bitmap images)
into web-viewable outputs.  Two operating modes:

  direct         — artefact_type ∈ {ACORN_SPRITE, ACORN_DRAW, ACORN_TEXT,
                   IMAGE}: convert the artefact's own file.
  extraction_scan — hints contain ``extraction_path``: walk the extraction
                   output directory and convert every viewable file found.
"""

import json
from pathlib import Path
from shared.enums import ArtefactType
from ..config import log
from ..tools import (
    convert_draw,
    convert_sprite,
    parse_acorn_filename,
)
from ._common import analysis_handler

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

# Raster image extensions that the NSFW classifier can read via Pillow.
# WMF/EMF are intentionally excluded — they are vector formats requiring
# wmf-cli pre-conversion before Pillow can open them.
_RASTER_EXTENSIONS: frozenset[str] = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.bmp', '.tif', '.tiff', '.pcx', '.tga',
})
# RISC OS filetypes for raster images (hex string, lower-case)
_RISC_OS_IMAGE_FILETYPES: frozenset[str] = frozenset({'c85', '695', 'b60', '69c', 'ff0'})

# Hard-coded model metadata — used when a sidecar JSON is missing.
# These are the correct values for the shipped models; update if the
# models are replaced.  Export scripts emit the JSON sidecars so manual
# installs can also fall back here.
_NSFW_META1_DEFAULT: dict = {
    'nsfw_class_index': 0,         # Marqo label_names[0] == 'NSFW'
    'input_size':       384,
    'mean':             [0.5, 0.5, 0.5],
    'std':              [0.5, 0.5, 0.5],
    'interpolation':    'bicubic',
    'crop_pct':         1.0,
}
_NSFW_META2_DEFAULT: dict = {
    'nsfw_class_index': 1,         # Prithiv id2label[1] == 'NSFW' (per model card)
    'input_size':       224,
    'mean':             [0.48145466, 0.4578275,  0.40821073],  # CLIP/MetaCLIP
    'std':              [0.26862954, 0.26130258, 0.27577711],
    'interpolation':    'bicubic',
    'crop_pct':         0.875,
}


def _load_nsfw_sessions(self) -> bool:
    """Load ONNX sessions and per-model sidecar metadata.  Returns True when ready.

    Idempotent: if sessions are already loaded this is a no-op.  Stores results
    on ``self`` as ``_nsfw_sess1/2``, ``_nsfw_input1/2``, ``_nsfw_meta1/2``.
    """
    if self._nsfw_sess1 is not None:
        return True

    import json
    from ..config import NSFW_MODEL_DIR, NSFW_QUANTIZE

    try:
        import onnxruntime as ort
    except ImportError:
        log.error('onnxruntime not installed — NSFW scanning unavailable')
        return False

    model_dir = NSFW_MODEL_DIR
    if NSFW_QUANTIZE:
        m1_path = model_dir / 'marqo.onnx'
        m2_path = model_dir / 'clip.onnx'
    else:
        m1_path = model_dir / 'marqo' / 'model.onnx'
        m2_path = model_dir / 'clip'  / 'model.onnx'

    try:
        self._nsfw_sess1  = ort.InferenceSession(str(m1_path))
        self._nsfw_input1 = self._nsfw_sess1.get_inputs()[0].name
        self._nsfw_sess2  = ort.InferenceSession(str(m2_path))
        self._nsfw_input2 = self._nsfw_sess2.get_inputs()[0].name
    except Exception as exc:
        log.error(f'Failed to load NSFW ONNX sessions: {exc}')
        return False

    meta1_path = model_dir / 'marqo_meta.json'
    if meta1_path.exists():
        self._nsfw_meta1 = json.loads(meta1_path.read_text())
        log.info(f'Loaded marqo_meta.json: nsfw_idx={self._nsfw_meta1["nsfw_class_index"]} '
                 f'mean={self._nsfw_meta1["mean"]}')
    else:
        log.warning('marqo_meta.json not found — using hard-coded defaults')
        self._nsfw_meta1 = _NSFW_META1_DEFAULT.copy()

    meta2_path = model_dir / 'clip_meta.json'
    if meta2_path.exists():
        self._nsfw_meta2 = json.loads(meta2_path.read_text())
        log.info(f'Loaded clip_meta.json: nsfw_idx={self._nsfw_meta2["nsfw_class_index"]} '
                 f'mean={self._nsfw_meta2["mean"]}')
    else:
        log.warning('clip_meta.json not found — using hard-coded defaults')
        self._nsfw_meta2 = _NSFW_META2_DEFAULT.copy()

    return True


def _convert_file_to_outputs(
    self,
    input_path: Path,
    artefact_type: 'ArtefactType',
    work_dir: Path,
    output_subdir: str | None,
    analysis_uuid: str,
    file_index: int = 0,
) -> tuple[list[dict] | None, str | None]:
    """
    Convert a single viewable file and return ``(outputs, error)``.

    On success: ``(list_of_output_dicts, None)``.
    On failure: ``(None, error_message)`` — caller should call
    ``fail_analysis`` (direct mode) or record the failure and continue (extraction_scan).

    ``file_index`` is used to make temporary subdirectory names unique when
    converting multiple files within one analysis run.
    """
    outputs = []

    if artefact_type == ArtefactType.ACORN_SPRITE:
        tmp_out = work_dir / f'sprites_{file_index}'
        result = convert_sprite(input_path, tmp_out, analysis_uuid)
        if not result['success']:
            log.warning(f"Sprite conversion failed for {input_path}: {result.get('error')}")
            return None, result.get('error') or 'Conversion failed'
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
        result = convert_draw(input_path, tmp_out, analysis_uuid)
        if not result['success']:
            return None, result.get('error') or 'Conversion failed'
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
            return None, str(e)

    elif artefact_type == ArtefactType.IMAGE:
        from ..tools.images_common import convert_image  # numpy/scour: worker-only
        true_name, _ = parse_acorn_filename(input_path.name)
        tmp_out = work_dir / f'image_{file_index}'
        result = convert_image(input_path, tmp_out, analysis_uuid)
        if not result['success']:
            log.warning(f"Image conversion failed for {input_path}: {result.get('error')}")
            return None, result.get('error') or 'Conversion failed'
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

    return outputs, None


def _detect_viewable_type(self, path: Path) -> 'ArtefactType | None':
    """Return the ArtefactType for a viewable file, or None if not viewable."""
    name_lower = path.name.lower()
    for suffix, atype in self._RISCOS_VIEWABLE_SUFFIXES.items():
        if name_lower.endswith(suffix):
            return atype
    return self._EXT_VIEWABLE.get(path.suffix.lower())


@analysis_handler("format conversion")
def process_format_convert(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process FORMAT_CONVERT analysis.  Supports two modes:

    direct — artefact_type is ACORN_SPRITE/DRAW/TEXT/IMAGE: convert the
      artefact's own file.  Used for directly-uploaded Acorn files;
      triggered via ANALYSIS_MAP.

    extraction_scan — hints contain 'extraction_path': scan the extraction
      output directory for every viewable file, convert each one, and store
      outputs with a 'source_file' field matching ExtractedFile.path
      (display path, Acorn filetype suffix stripped).  Queued automatically
      by queue_partition_follow_ups() after every FILE_EXTRACTION and
      ARCHIVE_EXTRACT.
    """
    analysis_id = analysis['id']
    analysis_uuid = analysis['uuid']
    artefact_type_str = artefact.get('artefact_type', '')
    hints = json.loads(analysis.get('hints') or '{}')

    item = artefact.get('item', {})
    item_uuid = item.get('uuid', '')
    item_slug = item.get('slug', '')
    artefact_uuid = artefact.get('uuid', '')
    artefact_slug = artefact.get('slug', '')
    item_part = f"{item_uuid}_{item_slug}" if item_slug else item_uuid
    artefact_part = f"{artefact_uuid}_{artefact_slug}" if artefact_slug else artefact_uuid
    output_subdir = f"{item_part}/{artefact_part}" if (item_part and artefact_part) else None

    _direct_types = (
        ArtefactType.ACORN_SPRITE.value,
        ArtefactType.ACORN_DRAW.value,
        ArtefactType.ACORN_TEXT.value,
        ArtefactType.IMAGE.value,
    )

    # --- direct: convert the artefact's own file ---
    if artefact_type_str in _direct_types:
        input_path = self.get_input_path(artefact, work_dir)
        artefact_type = ArtefactType(artefact_type_str)
        outputs, _ = self._convert_file_to_outputs(
            input_path, artefact_type, work_dir, output_subdir, analysis_uuid,
        )
        if outputs is None:
            self.fail_analysis(analysis_id, f'Conversion failed for {artefact_type_str}')
            return
        self.complete_analysis(
            analysis_id,
            summary=f'Converted {len(outputs)} output(s) for {artefact_type_str}',
            details=json.dumps({
                'artefact_type': artefact_type_str,
                'outputs': outputs,
            }),
        )
        if artefact_type == ArtefactType.IMAGE:
            from shared.enums import AnalysisType
            from ..config import NSFW_ENABLED
            if NSFW_ENABLED:
                self.api.queue_analysis(artefact_uuid, AnalysisType.NSFW_SCAN.value)
        return

    # --- extraction_scan: walk extraction output and convert viewable files ---
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
    viewable_files: list[tuple[dict, ArtefactType]] = []
    if partition_uuid:
        files_resp = self.api.get(
            f"/partitions/{partition_uuid}/files?per_page=10000&show_known=true"
        )
        all_files = files_resp.get('files', []) if files_resp else []

        # Filter to files in our extraction context.
        # Mirror the ARCHIVE_DETECT guard: when no path_prefix, only
        # process files directly from the disc image (depth 0), not from
        # nested archives which get their own scoped FORMAT_CONVERT job.
        if path_prefix:
            all_files = [f for f in all_files
                         if f.get('path', '').startswith(path_prefix + '/')]
        else:
            all_files = [f for f in all_files
                         if f.get('extraction_depth', 0) == 0]

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

        file_outputs, file_error = self._convert_file_to_outputs(
            file_path, viewable_type, work_dir, output_subdir, analysis_uuid, file_index,
        )
        file_index += 1
        if file_outputs is None:
            log.warning(f"Skipping {file_path} — conversion failed: {file_error}")
            failed_conversions.append({
                'source_file': display_path,
                'error': file_error or 'Conversion failed',
            })
            continue
        for out in file_outputs:
            out['source_file'] = display_path
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

    # Queue NSFW_SCAN to classify the converted PNG outputs.  Must be queued
    # here (after FORMAT_CONVERT completes) rather than alongside it, because
    # the PNGs don't exist until the conversion finishes.
    # SVG outputs (Draw files) are excluded — Pillow cannot read SVG.
    from ..config import NSFW_ENABLED
    if NSFW_ENABLED:
        from shared.enums import AnalysisType as _AT
        nsfw_fc_outputs = [
            {
                'path': o['filename'],
                'source_file': o['source_file'],
                **({'sprite_name': o['name']} if o.get('tool') == 'spritefile' and o.get('name') else {}),
            }
            for o in all_outputs
            if o.get('type') == 'image' and not o.get('filename', '').endswith('.svg')
        ]
        if nsfw_fc_outputs:
            fc_nsfw_hints: dict = {'format_convert_outputs': nsfw_fc_outputs}
            if partition_uuid:
                fc_nsfw_hints['partition_uuid'] = partition_uuid
            if path_prefix:
                fc_nsfw_hints['path_prefix'] = path_prefix
            self.api.queue_analysis(artefact_uuid, _AT.NSFW_SCAN.value, hints=fc_nsfw_hints)
@analysis_handler("NSFW scan")
def process_nsfw_scan(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process NSFW_SCAN analysis.  Supports three modes:

    direct — IMAGE artefact: classify the artefact's own file.
      Queued by process_format_convert() after converting a directly-uploaded image.

    extraction_scan — hints contain 'extraction_path': walk the extraction output
      and classify every raster image found.  Queued by queue_partition_follow_ups()
      after FILE_EXTRACTION / ARCHIVE_EXTRACT.  Handles direct JPEG/PNG/GIF files
      stored in the partition.

    format_convert_scan — hints contain 'format_convert_outputs': classify PNG
      outputs produced by FORMAT_CONVERT (converted from Sprite/Draw files).
      Queued by process_format_convert() after it finishes all conversions, so the
      PNGs are guaranteed to exist.  Avoids the race condition that occurs when
      extraction_scan runs before FORMAT_CONVERT has produced its outputs.
    """
    import json
    from ..config import (
        NSFW_AGREE_THRESHOLD,
        NSFW_COLOCATED,
        NSFW_ENABLED,
        NSFW_HIGH,
        NSFW_LOW,
        NSFW_MIN_PIXELS,
        NSFW_S1_MIN_EXPLICIT,
        NSFW_S2_THRESHOLD,
    )
    from ..tools.nsfw import classify_batch

    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')

    if not NSFW_ENABLED:
        self.complete_analysis(analysis_id, summary='NSFW scanning disabled')
        return

    if not self._load_nsfw_sessions():
        self.fail_analysis(analysis_id, 'NSFW model sessions could not be loaded')
        return

    format_convert_outputs = hints.get('format_convert_outputs')
    extraction_path = hints.get('extraction_path')
    partition_uuid  = hints.get('partition_uuid')
    path_prefix     = hints.get('path_prefix', '')

    if format_convert_outputs:
        # format_convert_scan: classify PNG outputs produced by FORMAT_CONVERT.
        # Each entry is {'path': relative_storage_path, 'source_file': db_path}.
        from shared.storage import LocalStorage
        image_paths: list[str] = []
        db_path_map: dict[str, str] = {}
        sprite_name_map: dict[str, str] = {}

        for item in format_convert_outputs:
            rel_path = item.get('path', '')
            source_file = item.get('source_file', rel_path)
            if not rel_path:
                continue

            if isinstance(self.storage, LocalStorage):
                local = self.outputs / rel_path
                if not local.exists():
                    log.warning(f'NSFW scan: format_convert output not found: {rel_path}')
                    continue
            else:
                key = self.storage.storage_key('outputs', rel_path)
                local = work_dir / Path(rel_path).name
                try:
                    self.storage.get(key, local)
                except FileNotFoundError:
                    log.warning(f'NSFW scan: format_convert output not found in storage: {key}')
                    continue

            local_str = str(local)
            image_paths.append(local_str)
            db_path_map[local_str] = source_file
            if item.get('sprite_name'):
                sprite_name_map[local_str] = item['sprite_name']

        raw_results = classify_batch(
            self._nsfw_sess1, self._nsfw_input1, self._nsfw_meta1,
            self._nsfw_sess2, self._nsfw_input2, self._nsfw_meta2,
            image_paths, NSFW_HIGH, NSFW_LOW, NSFW_MIN_PIXELS,
            s2_threshold=NSFW_S2_THRESHOLD, s1_min_explicit=NSFW_S1_MIN_EXPLICIT,
            agree_threshold=NSFW_AGREE_THRESHOLD, colocated_threshold=NSFW_COLOCATED,
        )
        results = [
            {
                **r,
                'source_file': db_path_map.get(r['path'], r['path']),
                **({'sprite_name': sprite_name_map[r['path']]} if r['path'] in sprite_name_map else {}),
            }
            for r in raw_results
        ]
        explicit_count = sum(1 for r in results if r['verdict'] == 'explicit')
        skipped_count  = sum(1 for r in results if r['verdict'] == 'skipped')
        scanned        = len(results) - skipped_count
        self.complete_analysis(
            analysis_id,
            summary=(
                f'Scanned {scanned} of {len(image_paths)} converted image(s): '
                f'{explicit_count} explicit'
                + (f', {skipped_count} skipped' if skipped_count else '')
            ),
            details=json.dumps({
                'mode':           'format_convert_scan',
                'path_prefix':    path_prefix or None,
                'found':          len(image_paths),
                'scanned':        scanned,
                'skipped_count':  skipped_count,
                'explicit_count': explicit_count,
                'results':        results,
            }),
        )
        if partition_uuid and explicit_count:
            explicit_restrictions = [
                {
                    'path':             r['source_file'],
                    'restriction_type': 'explicit',
                    'reason':           f'NSFW classifier stage {r["stage"]}: score={r["score"]:.3f}',
                }
                for r in results if r['verdict'] == 'explicit'
            ]
            resp = self.api.apply_file_restrictions(partition_uuid, explicit_restrictions)
            if resp:
                log.info(
                    f'NSFW scan: applied {resp.get("applied", 0)} new, '
                    f'updated {resp.get("updated", 0)} existing restrictions '
                    f'({resp.get("not_found", 0)} paths not found in partition)'
                )

    elif extraction_path:
        # extraction_scan: classify raster images in the extraction output.
        # Handles JPEG/PNG/GIF/etc. files stored directly in the partition
        # (as opposed to Sprite/Draw files which need FORMAT_CONVERT first).
        image_paths = []
        db_path_map = {}

        if partition_uuid:
            files_resp = self.api.get(
                f'/partitions/{partition_uuid}/files?per_page=10000&show_known=true'
            )
            all_files = files_resp.get('files', []) if files_resp else []

            if path_prefix:
                all_files = [f for f in all_files
                             if f.get('path', '').startswith(path_prefix + '/')]
            else:
                all_files = [f for f in all_files
                             if f.get('extraction_depth', 0) == 0]

            for file_data in all_files:
                if file_data.get('is_directory', False):
                    continue
                fname = file_data.get('filename') or file_data.get('path', '')
                ext   = Path(fname).suffix.lower()
                ft    = (file_data.get('risc_os_filetype') or '').lower()
                if ext not in _RASTER_EXTENSIONS and ft not in _RISC_OS_IMAGE_FILETYPES:
                    continue

                db_path   = file_data['path']
                disk_path = (db_path[len(path_prefix) + 1:]
                             if path_prefix and db_path.startswith(path_prefix + '/')
                             else db_path)

                local = self._resolve_single_extraction_file(
                    extraction_path, disk_path, work_dir,
                    risc_os_filetype=file_data.get('risc_os_filetype') or None,
                )
                if local is None and disk_path != db_path:
                    local = self._resolve_single_extraction_file(
                        extraction_path, db_path, work_dir,
                        risc_os_filetype=file_data.get('risc_os_filetype') or None,
                    )
                if local is None:
                    log.warning(f'NSFW scan: image not found on disk: {db_path}')
                    continue
                local_str = str(local)
                image_paths.append(local_str)
                db_path_map[local_str] = db_path

        raw_results = classify_batch(
            self._nsfw_sess1, self._nsfw_input1, self._nsfw_meta1,
            self._nsfw_sess2, self._nsfw_input2, self._nsfw_meta2,
            image_paths, NSFW_HIGH, NSFW_LOW, NSFW_MIN_PIXELS,
            s2_threshold=NSFW_S2_THRESHOLD, s1_min_explicit=NSFW_S1_MIN_EXPLICIT,
            agree_threshold=NSFW_AGREE_THRESHOLD, colocated_threshold=NSFW_COLOCATED,
        )
        results = [
            {**r, 'source_file': db_path_map.get(r['path'], r['path'])}
            for r in raw_results
        ]
        explicit_count = sum(1 for r in results if r['verdict'] == 'explicit')
        skipped_count  = sum(1 for r in results if r['verdict'] == 'skipped')
        scanned        = len(results) - skipped_count
        self.complete_analysis(
            analysis_id,
            summary=(
                f'Scanned {scanned} of {len(image_paths)} image(s): '
                f'{explicit_count} explicit'
                + (f', {skipped_count} skipped' if skipped_count else '')
            ),
            details=json.dumps({
                'mode':           'extraction_scan',
                'path_prefix':    path_prefix or None,
                'found':          len(image_paths),
                'scanned':        scanned,
                'skipped_count':  skipped_count,
                'explicit_count': explicit_count,
                'results':        results,
            }),
        )
        if partition_uuid and explicit_count:
            explicit_restrictions = [
                {
                    'path':             r['source_file'],
                    'restriction_type': 'explicit',
                    'reason':           f'NSFW classifier stage {r["stage"]}: score={r["score"]:.3f}',
                }
                for r in results if r['verdict'] == 'explicit'
            ]
            resp = self.api.apply_file_restrictions(partition_uuid, explicit_restrictions)
            if resp:
                log.info(
                    f'NSFW scan: applied {resp.get("applied", 0)} new, '
                    f'updated {resp.get("updated", 0)} existing restrictions '
                    f'({resp.get("not_found", 0)} paths not found in partition)'
                )

    else:
        # direct: classify the artefact file directly.
        input_path = self.get_input_path(artefact, work_dir)
        raw_results = classify_batch(
            self._nsfw_sess1, self._nsfw_input1, self._nsfw_meta1,
            self._nsfw_sess2, self._nsfw_input2, self._nsfw_meta2,
            [str(input_path)], NSFW_HIGH, NSFW_LOW, NSFW_MIN_PIXELS,
            s2_threshold=NSFW_S2_THRESHOLD, s1_min_explicit=NSFW_S1_MIN_EXPLICIT,
            agree_threshold=NSFW_AGREE_THRESHOLD, colocated_threshold=NSFW_COLOCATED,
        )
        if raw_results:
            r = raw_results[0]
            if r['verdict'] == 'skipped':
                summary = f'Image skipped ({r["reason"]})'
            else:
                summary = f'Stage {r["stage"]}: {r["verdict"]} (score={r["score"]:.3f})'
        else:
            summary = 'No result'
        self.complete_analysis(
            analysis_id,
            summary=summary,
            details=json.dumps({'mode': 'direct', 'results': raw_results}),
        )


# vim: ts=4 sw=4 et
