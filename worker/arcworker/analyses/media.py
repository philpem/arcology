"""
MEDIA_TRANSCODE analysis handler.

Makes generic time-based media (audio/video) playable in the viewer, and
records ffprobe codec/track metadata for it (mirroring Acorn Replay movies).

Every media file is probed with ffprobe.  Whether it is re-encoded depends on
the container **and** the codecs inside it (``media_is_browser_playable``):

* **Passthrough** — anything a modern browser can already play (e.g. an MP4 or
  MOV with H.264/AAC, a WebM, an MP3/OGG/FLAC) is left untouched; the viewer
  streams the original bytes.  We still record its metadata and, for video,
  grab a first-frame poster.
* **Transcode** — anything browsers cannot play (AVI, MPEG-1/2, DivX/Xvid, WMV,
  HEVC-in-MP4, …) is re-encoded to a browser-playable H.264/AAC MP4 (video) or
  AAC M4A (audio-only).

Two modes, mirroring FORMAT_CONVERT:

Mode 1 — Direct artefact (artefact_type VIDEO/AUDIO): probe/transcode the
  artefact's own uploaded file.

Mode 2 — Extraction scan (partition_uuid hint): scan the extraction for media
  files (via the shared batch scaffold) and probe/transcode each.
"""

import json
from pathlib import Path
from arcology_shared.artefact_types import (
    MEDIA_EXTENSIONS,
    RISCOS_FILETYPE_EXTENSION,
    media_is_browser_playable,
    media_kind_for_extension,
    media_kind_for_riscos_filetype,
)
from arcology_shared.enums import AnalysisType, ArtefactType
from ..config import log
from ..tools import (
    extract_media_poster,
    probe_media,
    transcode_media_to_audio,
    transcode_media_to_mp4,
)
from ..utils.paths import artefact_output_subdir
from ._common import (
    analysis_handler,
    iter_resolved_files,
    scan_partition_files,
    transcode_cached,
)


def _process_media_file(self, file_path: Path, db_path: str | None, filename: str,
                        base_name: str, work_dir: Path, output_subdir: str) -> dict:
    """Probe one media file and transcode it only if browsers can't play it.

    Returns a result entry dict on success, or a dict with an ``error`` key on
    failure.  ``mp4_output_path`` is set only when the file was transcoded;
    passthrough files keep it ``None`` and are played from their original bytes.
    The ffprobe metadata is always included so the web side can show it.
    """
    probe = probe_media(file_path)
    if not probe.get('success'):
        return {'file_path': db_path, 'error': probe.get('error', 'ffprobe failed')}

    has_video = probe.get('has_video', False)
    has_audio = probe.get('has_audio', False)
    media_kind = 'video' if has_video else 'audio'
    playable = media_is_browser_playable(
        filename,
        has_video=has_video,
        video_codec=probe.get('video_codec'),
        audio_codec=probe.get('audio_codec'),
    )

    entry = {
        'file_path': db_path,
        'media_kind': media_kind,
        'container_format': probe.get('container_format'),
        'video_codec': probe.get('video_codec'),
        'width': probe.get('width'),
        'height': probe.get('height'),
        'frame_rate': probe.get('frame_rate'),
        'audio_codec': probe.get('audio_codec'),
        'sample_rate': probe.get('sample_rate'),
        'channels': probe.get('channels'),
        'has_audio': has_audio,
        'duration_seconds': probe.get('duration_seconds'),
        'mp4_output_path': None,
        'poster_path': None,
        'passthrough': playable,
    }

    if playable:
        # No re-encode — the viewer streams the original bytes.  Grab a poster
        # for video so the grid has a thumbnail.
        if has_video:
            poster_name = f'{base_name}_poster.jpg'
            poster_path = work_dir / poster_name
            made = extract_media_poster(file_path, poster_path)
            if made:
                entry['poster_path'] = self.save_output_file(
                    poster_path, poster_name, subdir=output_subdir)
        return entry

    # Transcode to a browser-playable format — content-keyed so byte-identical
    # source media is only ever encoded once (transcode_cached stores the output
    # under a content-addressed path and skips the re-encode on a later hit).
    output_ext = 'mp4' if has_video else 'm4a'
    produce_error: dict = {}

    def _produce():
        out_path = work_dir / f'{base_name}.{output_ext}'
        if has_video:
            poster_path = work_dir / f'{base_name}_poster.jpg'
            result = transcode_media_to_mp4(
                file_path, out_path, work_dir=work_dir,
                has_audio=has_audio, poster_path=poster_path,
            )
        else:
            result = transcode_media_to_audio(file_path, out_path, work_dir=work_dir)
        if not result.get('success'):
            produce_error.update(
                error=result.get('error', 'Transcode failed'),
                stage=result.get('stage'))
            return None, None
        local_poster = result.get('poster_path')
        return out_path, (Path(local_poster) if local_poster else None)

    cached = transcode_cached(
        self, input_path=file_path, output_ext=output_ext, produce=_produce)
    if cached is None:
        return {
            'file_path': db_path,
            'media_kind': media_kind,
            'error': produce_error.get('error', 'Transcode failed'),
            'stage': produce_error.get('stage'),
        }

    # Carry through the output paths plus (on a cache miss) the produced files'
    # hashes so the web side can register the refcounting OutputBlob rows.
    entry.update({k: v for k, v in cached.items() if k != 'cache_hit'})
    return entry


@analysis_handler("Transcode media", AnalysisType.MEDIA_TRANSCODE)
def process_media_transcode(self, analysis: dict, artefact: dict, work_dir: Path):
    """Probe media files and transcode the ones browsers cannot play."""
    analysis_id = analysis['id']
    analysis_uuid = analysis['uuid']
    artefact_type_str = artefact.get('artefact_type', '')
    output_subdir = artefact_output_subdir(artefact)

    _media_types = (ArtefactType.VIDEO.value, ArtefactType.AUDIO.value)

    # --- Mode 1: Direct artefact ---
    if artefact_type_str in _media_types:
        original_filename = artefact.get('original_filename', '') or ''
        input_path = self.get_input_path(artefact, work_dir)
        entry = _process_media_file(
            self, input_path, None, original_filename,
            analysis_uuid, work_dir, output_subdir,
        )
        if 'error' in entry and 'media_kind' not in entry:
            # Probe failure — nothing usable.
            self.fail_analysis(analysis_id, f'Media probe failed: {entry["error"]}')
            return
        if 'error' in entry:
            self.fail_analysis(analysis_id, f'Media transcode failed: {entry["error"]}')
            return
        kind = 'passthrough' if entry.get('passthrough') else 'transcoded'
        self.complete_analysis(
            analysis_id,
            tool_name='ffmpeg',
            summary=f'Processed media ({kind})',
            details=json.dumps({'transcoded': [entry], 'transcode_errors': []}),
        )
        return

    # --- Mode 2: Extraction scan ---
    def _select(file_data: dict) -> bool:
        ext = Path(file_data.get('filename', '')).suffix.lower()
        if ext in MEDIA_EXTENSIONS:
            return True
        filetype = (file_data.get('risc_os_filetype') or '').lower()
        return media_kind_for_riscos_filetype(filetype) is not None

    scan = scan_partition_files(self, analysis, artefact, select_files=_select)
    if scan is None:
        self.fail_analysis(
            analysis_id,
            'No partition_uuid in hints or could not determine extraction path',
        )
        return

    if not scan.files:
        self.complete_analysis(
            analysis_id,
            summary='No media files found',
            details=json.dumps({'transcoded': [], 'files_scanned': 0}),
        )
        return

    processed = []
    errors = []

    def _missing(file_data, db_path):
        log.warning(f"Media file not found on disk: {db_path}")
        errors.append({'file_path': db_path, 'error': 'File not found on disk'})

    for index, (file_data, file_path, db_path) in enumerate(iter_resolved_files(
            self, scan.files, scan.extraction_path, work_dir,
            path_prefix=scan.path_prefix, on_missing=_missing)):
        base_name = f'{analysis_uuid}_{index}'
        filename = file_data.get('filename', '')
        # For RISC OS files without a PC-style extension, synthesise one so that
        # media_is_browser_playable() makes the right passthrough decision
        # (e.g. a RISC OS MP4 &A64 without ".mp4" would otherwise always transcode).
        if not Path(filename).suffix:
            ft = (file_data.get('risc_os_filetype') or '').lower()
            synth_ext = RISCOS_FILETYPE_EXTENSION.get(ft, '')
            if synth_ext:
                filename = 'file' + synth_ext
        entry = _process_media_file(
            self, file_path, db_path, filename,
            base_name, work_dir, output_subdir,
        )
        if 'error' in entry:
            log.warning(f"Skipping {db_path} — {entry['error']}")
            entry.setdefault('media_kind',
                media_kind_for_extension(Path(db_path).suffix) or
                media_kind_for_riscos_filetype((file_data.get('risc_os_filetype') or '').lower())
            )
            errors.append(entry)
            continue
        processed.append(entry)

    transcoded_count = sum(1 for e in processed if e.get('mp4_output_path'))
    passthrough_count = len(processed) - transcoded_count
    summary_parts = [
        f'Processed {len(processed)} media file(s) '
        f'({transcoded_count} transcoded, {passthrough_count} passthrough)'
    ]
    if errors:
        summary_parts.append(f'{len(errors)} failed')

    details_dict: dict = {
        'transcoded': processed,
        'transcode_errors': errors,
        'files_scanned': len(scan.files),
    }
    if scan.path_prefix:
        details_dict['path_prefix'] = scan.path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='ffmpeg',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )

# vim: ts=4 sw=4 et
