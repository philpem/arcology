"""
Metadata-style analysis handlers.

Covers:
  CHECKSUM_COMPUTE     — MD5/SHA256 hashes for the artefact file.
  METADATA_EXTRACT     — format-specific metadata (currently ISO 9660 PVD).
  FORMAT_IDENTIFY      — sniff archive / FCFS images that lack the right
                         filename, queue extraction or convert FCFS.
  PRODUCT_RECOGNITION  — fold extracted files against hash-database product
                         definitions.
  RISCOS_MODULE_PARSE  — parse RISC OS relocatable modules (filetype FFA).
  REPLAY_PROCESS       — index Acorn Replay / ARMovie files (filetype AE7):
                         parse the header AND transcode the video to MP4.
"""

import json
from pathlib import Path
from arcology_shared.content_categories import ContentCategory, classify_content
from arcology_shared.enums import AnalysisType, ArtefactType
from arcology_shared.fuzzyhash import compute_tlsh
from ..config import REPLAY_MODULES_DIR, log
from ..tools import (
    ArmovieParseError,
    ModuleParseError,
    compute_file_hash,
    convert_replay_poster_sprite,
    decode_module,
    file_has_armovie_magic,
    parse_armovie_header,
    read_file_capped,
    transcode_armovie_to_audio,
    transcode_armovie_to_mp4,
)
from ..tools.extraction import convert_fcfs_to_raw
from ..tools.iso9660 import parse_iso9660_pvd
from ._common import (
    analysis_handler,
    iter_resolved_files,
    scan_partition_files,
    transcode_cached,
)

# Sanity cap on an ARMovie embedded poster sprite (a small RISC OS spritefile
# thumbnail).  A header claiming more than this is corrupt/hostile, so we skip
# the poster rather than read the bytes into RAM.
_MAX_POSTER_SPRITE_BYTES = 16 * 1024 * 1024

# Flux artefact types whose raw bytes carry timing noise — a byte-level fuzzy
# hash (TLSH) is meaningless for them, so it is skipped.
_TLSH_SKIP_TYPES = frozenset({
    ArtefactType.SCP.value,
    ArtefactType.DFI.value,
    ArtefactType.A2R.value,
})


@analysis_handler("checksum computation", AnalysisType.CHECKSUM_COMPUTE)
def process_checksum_compute(self, analysis: dict, artefact: dict, work_dir: Path):
    """Compute MD5 and SHA256 hashes for the artefact file and store them."""
    analysis_id = analysis['id']
    input_path = self.get_input_path(artefact, work_dir)

    md5, sha256, size = compute_file_hash(input_path)
    # Byte-level fuzzy hash for near-duplicate detection, skipped for flux types.
    tlsh = None
    if artefact.get('artefact_type') not in _TLSH_SKIP_TYPES:
        tlsh = compute_tlsh(input_path)
    self.api.update_artefact_hashes(artefact['uuid'], md5, sha256, tlsh=tlsh)

    details = {'md5': md5, 'sha256': sha256, 'size': size}
    if tlsh:
        details['tlsh'] = tlsh
    self.complete_analysis(
        analysis_id,
        summary=f'MD5: {md5}  SHA256: {sha256}',
        details=json.dumps(details),
    )


@analysis_handler("metadata extraction", AnalysisType.METADATA_EXTRACT)
def process_metadata_extract(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process METADATA_EXTRACT analysis.
    Extracts format-specific metadata.

    For ISO 9660 images, parses the Primary Volume Descriptor to pull
    out volume/publisher/preparer strings and the creation/modification
    timestamps.  The PVD fields are both attached to the analysis
    details (for history) and written back to the artefact's
    ``media_metadata`` JSON column (for fast display on the artefact
    page).
    """
    analysis_id = analysis['id']

    input_path = self.get_input_path(artefact, work_dir)
    artefact_type = artefact['artefact_type']

    metadata = {}

    # Get basic file info
    md5, sha256, size = compute_file_hash(input_path)
    metadata['file'] = {
        'size': size,
        'md5': md5,
        'sha256': sha256
    }

    summary = f'Extracted metadata for {artefact_type}'

    if artefact_type == ArtefactType.ISO.value:
        iso9660 = parse_iso9660_pvd(input_path)
        if iso9660:
            metadata['iso9660'] = iso9660
            self.api.update_artefact_media_metadata(
                artefact['uuid'], {'iso9660': iso9660}
            )
            vol = iso9660.get('volume_identifier')
            if vol:
                summary = f'ISO 9660 volume: {vol}'
            else:
                summary = 'Parsed ISO 9660 Primary Volume Descriptor'

    self.complete_analysis(
        analysis_id,
        summary=summary,
        details=json.dumps(metadata)
    )


@analysis_handler("file format identification", AnalysisType.FORMAT_IDENTIFY)
def process_format_identify(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process FORMAT_IDENTIFY analysis.
    Attempts to identify the exact format of an image.

    Currently handles:
    - FCFS hard disk images (FileCore Filing System, RISC OS filetype &FCD).
      Detected via the 4-byte magic "FCFS" at file_size - 256.
      Converts to a raw sector image with fcfs2raw, then registers the
      result as a derived RAW_SECTOR artefact so that PARTITION_DETECT
      and FILE_EXTRACTION run automatically.
    """
    analysis_id = analysis['id']
    input_path = self.get_input_path(artefact, work_dir)
    artefact_label = artefact.get('label', 'image')

    # -------------------------------------------------------------------
    # FCFS detection: 4-byte magic "FCFS" at offset file_size - 256.
    # This is the standard FCFS trailer location documented in fcfs2raw.c.
    # -------------------------------------------------------------------
    detected_format = 'unknown'
    file_size = input_path.stat().st_size
    if file_size >= 256:
        try:
            with open(input_path, 'rb') as f:
                f.seek(file_size - 256)
                magic = f.read(4)
            if magic == b'FCFS':
                detected_format = 'fcfs'
        except OSError:
            pass

    if detected_format == 'fcfs':
        raw_path = work_dir / 'converted.img'
        conv_result = convert_fcfs_to_raw(input_path, raw_path)

        if not conv_result['success']:
            self.fail_analysis(
                analysis_id,
                f'FCFS detected but conversion failed: {conv_result.get("error", "unknown")}',
                tool_name='fcfs2raw',
                details=json.dumps({'detected': 'fcfs', 'fcfs2raw': conv_result})
            )
            return

        # Register derived RAW_SECTOR artefact.  auto_analyse=True causes
        # the web app to queue PARTITION_DETECT automatically, which in
        # turn queues FILE_EXTRACTION once partitions are mapped.
        derived = self.api.register_derived_artefact(
            analysis_id,
            f"{artefact_label} (raw sectors)",
            raw_path,
            ArtefactType.RAW_SECTOR,
            auto_analyse=True
        )

        self.complete_analysis(
            analysis_id,
            tool_name='fcfs2raw',
            summary='Identified as FCFS hard disk image; converted to raw sectors',
            details=json.dumps({
                'detected': 'fcfs',
                'fcfs2raw': conv_result,
                'derived_artefact': derived,
            })
        )
        return

    # Archive magic detection: catch files uploaded without a recognised
    # extension (e.g. a bare X-Files or TBAFS archive with no ".b23"/".b21"
    # suffix).  Uses the same signature table as the extraction pipeline so
    # every archive type that can be extracted is also detectable here.
    sniffed = self._sniff_archive_magic(input_path)
    if sniffed is not None:
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.ARCHIVE_EXTRACT.value,
            hints={'archive_type': sniffed.value},
        )
        self.complete_analysis(
            analysis_id,
            summary=f'Identified as {sniffed.value} archive by magic bytes; queued extraction',
            details=json.dumps({'detected': sniffed.value}),
        )
        return

    # No format recognised.
    self.complete_analysis(
        analysis_id,
        summary='Format not identified',
        details=json.dumps({'detected': 'unknown'})
    )


@analysis_handler("RISC OS module parse", AnalysisType.RISCOS_MODULE_PARSE)
def process_riscos_module_parse(self, analysis: dict, artefact: dict, work_dir: Path):
    """Parse RISC OS relocatable modules found in an extraction.

    Scans partition files for filetype ffa (Module), reads each from disk,
    and extracts metadata (title, version, date, SWIs, star commands).
    Only queued for Acorn filesystem extractions.
    """
    analysis_id = analysis['id']

    # Discover filetype ffa (Module) files via the shared batch scaffold.
    scan = scan_partition_files(
        self, analysis, artefact,
        select_files=lambda f: ContentCategory.RISCOS_MODULE in classify_content(
            f.get('filename') or f.get('path') or '', f.get('risc_os_filetype')),
    )
    if scan is None:
        self.fail_analysis(
            analysis_id,
            'No partition_uuid in hints or could not determine extraction path',
        )
        return

    if not scan.files:
        self.complete_analysis(
            analysis_id,
            summary='No RISC OS modules (filetype ffa) found',
            details=json.dumps({'modules': [], 'files_scanned': 0}),
        )
        return

    modules = []
    parse_errors = 0

    def _missing(file_data, db_path):
        nonlocal parse_errors
        log.warning(f"Module file not found on disk: {db_path}")
        parse_errors += 1

    for _file_data, file_path, db_path in iter_resolved_files(
            self, scan.files, scan.extraction_path, work_dir,
            path_prefix=scan.path_prefix, on_missing=_missing):
        try:
            # Capped read: a RISC OS module is small; refuse to slurp an
            # over-size (corrupt/hostile) file into RAM.
            data = read_file_capped(file_path)
            result = decode_module(data)
            result['file_path'] = db_path
            # Exclude the raw help_string (redundant with help_title)
            result.pop('help_string', None)
            modules.append(result)
        except ModuleParseError as e:
            log.warning(f"Could not parse module {db_path}: {e}")
            parse_errors += 1
        except Exception as e:
            log.warning(f"Unexpected error parsing module {db_path}: {e}")
            parse_errors += 1

    summary_parts = [f'Parsed {len(modules)} RISC OS module(s)']
    if parse_errors:
        summary_parts.append(f'{parse_errors} could not be parsed')

    details_dict: dict = {
        'modules': modules,
        'files_scanned': len(scan.files),
        'parse_errors': parse_errors,
    }
    if scan.path_prefix:
        details_dict['path_prefix'] = scan.path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='riscos_module_parser',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )


@analysis_handler("Process Acorn Replay file", AnalysisType.REPLAY_PROCESS)
def process_replay(self, analysis: dict, artefact: dict, work_dir: Path):
    """Index and transcode Acorn Replay / ARMovie files found in an extraction.

    Scans partition files for ARMovie movies (RISC OS filetype &AE7 or a
    PC-style ``.rpl`` / ``.replay`` extension) and, for each one, parses the text
    header + chunk catalogue into searchable metadata *and* transcodes the video
    to a browser-playable MP4 (or an M4A for sound-only movies) — both in a
    single pass.  These were once two jobs (REPLAY_PROCESS then REPLAY_TRANSCODE)
    that each re-discovered, re-magic-checked and re-parsed the very same files;
    fusing them removes the duplicate scan and the cross-job ordering hazard (the
    metadata row and its MP4 are now produced together, so the web side creates a
    fully-populated ReplayMovie row in one step).

    Best-effort per file: a movie whose codec needs a RISC OS decompressor module
    that is not available is recorded as a transcode error but *still* contributes
    its parsed metadata row (with no MP4).  A file selected only by its extension
    that turns out not to be ARMovie is skipped silently.

    Only meaningful for extractions that contain ARMovie files; a harmless no-op
    otherwise.
    """
    analysis_id = analysis['id']

    # Discover ARMovie files (filetype &AE7 or a Replay extension) via the
    # shared batch scaffold.
    scan = scan_partition_files(
        self, analysis, artefact,
        select_files=lambda f: ContentCategory.REPLAY in classify_content(
            f.get('filename') or f.get('path') or '', f.get('risc_os_filetype')),
    )
    if scan is None:
        self.fail_analysis(
            analysis_id,
            'No partition_uuid in hints or could not determine extraction path',
        )
        return

    if not scan.files:
        self.complete_analysis(
            analysis_id,
            summary='No Acorn Replay / ARMovie files (filetype ae7) found',
            details=json.dumps({'movies': [], 'files_scanned': 0}),
        )
        return

    path_prefix = scan.path_prefix
    # Only pass --modules-dir when it actually exists (it won't when the worker
    # runs outside the Docker image that bundles the codecs).
    modules_dir = REPLAY_MODULES_DIR if REPLAY_MODULES_DIR and Path(REPLAY_MODULES_DIR).is_dir() else None

    movies = []
    transcode_errors = []
    parse_errors = 0
    not_armovie = 0

    def _missing(file_data, db_path):
        log.warning(f"ARMovie file not found on disk: {db_path}")
        transcode_errors.append({'file_path': db_path, 'error': 'File not found on disk'})

    for index, (_file_data, file_path, db_path) in enumerate(iter_resolved_files(
            self, scan.files, scan.extraction_path, work_dir,
            path_prefix=path_prefix, on_missing=_missing)):

        # Confirm the ARMovie magic before parsing/transcoding — a file selected
        # only by its extension (no &AE7 filetype) might not be Replay at all.
        if not file_has_armovie_magic(file_path):
            log.info(f"Skipping {db_path}: no ARMovie magic (not an Acorn Replay file)")
            not_armovie += 1
            continue

        # The header drives both the searchable metadata row and ffmpeg's frame
        # geometry; the raw bytes are also reused for the embedded poster sprite.
        try:
            header = parse_armovie_header(file_path)
        except ArmovieParseError as e:
            log.warning(f"Could not parse ARMovie {db_path}: {e}")
            parse_errors += 1
            continue
        except Exception as e:
            log.warning(f"Unexpected error parsing ARMovie {db_path}: {e}")
            parse_errors += 1
            continue

        # The full parsed header IS the searchable metadata; transcode outputs
        # (mp4_output_path / poster_path / blob hashes) are merged in on success.
        entry = dict(header)
        entry['file_path'] = db_path

        base_name = f'{analysis["uuid"]}_{index}'
        produce_error: dict = {}

        if header.get('video_format') == 0:
            # Sound-only movie — no video frames, but still playable as audio.
            # Many sound-only Replay files carry a poster sprite (a title card);
            # extract it so the audio player and grid have a thumbnail.  Routed
            # through transcode_cached so identical ARMovie content is only
            # decoded once across artefacts.
            def _produce_audio(file_path=file_path, header=header,
                               base_name=base_name, produce_error=produce_error):
                audio_path = work_dir / f'{base_name}.m4a'
                result = transcode_armovie_to_audio(
                    file_path, audio_path,
                    work_dir=work_dir,
                    modules_dir=modules_dir,
                )
                if not result['success']:
                    produce_error.update(
                        error=result.get('error', 'Audio transcode failed'),
                        stage=result.get('stage'))
                    return None, None
                poster = _make_replay_poster(file_path, header, work_dir, base_name)
                return audio_path, poster

            cached = transcode_cached(
                self, input_path=file_path, output_ext='m4a', produce=_produce_audio)
            if cached is None:
                log.warning(f"Audio transcode failed for {db_path}: {produce_error.get('error')}")
                transcode_errors.append({
                    'file_path': db_path,
                    'error': produce_error.get('error', 'Audio transcode failed'),
                    'stage': produce_error.get('stage'),
                })
                # Metadata row is still recorded — only the MP4/poster is missing.
                movies.append(entry)
                continue
            entry['audio_only'] = True
            entry['has_audio'] = True
            entry.update({k: v for k, v in cached.items() if k != 'cache_hit'})
            movies.append(entry)
            continue

        # Prefer the author-supplied embedded poster sprite as the thumbnail; only
        # fall back to ffmpeg's first decoded frame when there is no poster sprite.
        def _produce_video(file_path=file_path, header=header,
                           base_name=base_name, produce_error=produce_error):
            mp4_path = work_dir / f'{base_name}.mp4'
            frame_path = work_dir / f'{base_name}.jpg'
            sprite_poster = _make_replay_poster(file_path, header, work_dir, base_name)
            result = transcode_armovie_to_mp4(
                file_path, mp4_path,
                width=header.get('width'),
                height=header.get('height'),
                frame_rate=header.get('frame_rate'),
                work_dir=work_dir,
                modules_dir=modules_dir,
                poster_path=None if sprite_poster else frame_path,
            )
            if not result['success']:
                produce_error.update(
                    error=result.get('error', 'Transcode failed'),
                    stage=result.get('stage'),
                    has_audio=result.get('has_audio', False))
                return None, None
            produce_error.update(has_audio=result.get('has_audio', False))
            poster = sprite_poster or (frame_path if result.get('poster_path') else None)
            return mp4_path, poster

        cached = transcode_cached(
            self, input_path=file_path, output_ext='mp4', produce=_produce_video)
        if cached is None:
            log.warning(f"Transcode failed for {db_path}: {produce_error.get('error')}")
            transcode_errors.append({
                'file_path': db_path,
                'error': produce_error.get('error', 'Transcode failed'),
                'stage': produce_error.get('stage'),
            })
            # Metadata row is still recorded — only the MP4/poster is missing.
            movies.append(entry)
            continue

        # has_audio is decided at encode time (whether a WAV track was decoded),
        # so it is unknown on a cache hit where produce() never ran — report None
        # rather than a possibly-wrong False.  Informational only (no DB column).
        entry['has_audio'] = (None if cached.get('cache_hit')
                              else produce_error.get('has_audio', False))
        entry.update({k: v for k, v in cached.items() if k != 'cache_hit'})
        movies.append(entry)

    transcoded_n = sum(1 for m in movies if m.get('mp4_output_path'))
    summary_parts = [f'Indexed {len(movies)} Acorn Replay / ARMovie file(s)',
                     f'{transcoded_n} transcoded']
    if transcode_errors:
        summary_parts.append(f'{len(transcode_errors)} transcode failed')
    if parse_errors:
        summary_parts.append(f'{parse_errors} could not be parsed')

    details_dict: dict = {
        'movies': movies,
        'files_scanned': len(scan.files),
        'transcode_errors': transcode_errors,
    }
    if parse_errors:
        details_dict['parse_errors'] = parse_errors
    if not_armovie:
        details_dict['not_armovie'] = not_armovie
    if path_prefix:
        details_dict['path_prefix'] = path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='armovie_parser,replay-transcode,ffmpeg',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )


def _make_replay_poster(file_path, header, work_dir, base_name):
    """Extract an ARMovie embedded poster sprite to a local PNG.

    Returns the local poster Path, or None when the movie has no embedded poster
    sprite or extraction failed.  Best-effort: a poster is a nicety, never a
    reason to fail the transcode.  The caller stores it (content-addressed,
    alongside the transcoded video) via :func:`transcode_cached`.

    The header tells us exactly where the poster sprite lives and how big it is,
    so we seek to it and read just those bytes (bounded by a sanity cap) — a
    multi-GB Replay file is never loaded into RAM to grab its thumbnail.
    """
    sprite_offset = header.get('sprite_offset')
    sprite_size = header.get('sprite_size')
    if not sprite_size or sprite_size <= 0 or sprite_offset is None or sprite_offset < 0:
        return None
    if sprite_size > _MAX_POSTER_SPRITE_BYTES:
        log.info(
            "Replay poster sprite for %s is implausibly large (%d bytes); skipping",
            base_name, sprite_size,
        )
        return None

    try:
        with open(file_path, 'rb') as f:
            f.seek(sprite_offset)
            sprite_bytes = f.read(sprite_size)
    except OSError as e:
        log.info("Could not read Replay poster sprite for %s: %s", base_name, e)
        return None

    poster_name = f'{base_name}_poster.png'
    poster_path = work_dir / poster_name
    # The blob already starts at the sprite, so pass offset 0; a short read
    # (truncated file) is caught by convert_replay_poster_sprite's bounds check.
    result = convert_replay_poster_sprite(sprite_bytes, 0, sprite_size, poster_path)
    if not result.get('success'):
        log.info("Replay poster sprite extraction failed for %s: %s", base_name, result.get('error'))
        return None
    return poster_path

# vim: ts=4 sw=4 et
