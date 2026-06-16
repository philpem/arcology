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
  REPLAY_PROCESS       — parse Acorn Replay / ARMovie files (filetype AE7).
"""

import json
from pathlib import Path
from arcology_shared.enums import AnalysisType, ArtefactType
from arcology_shared.hints import HintKey
from ..config import REPLAY_MODULES_DIR, log
from ..tools import (
    ArmovieParseError,
    ModuleParseError,
    compute_file_hash,
    convert_replay_poster_sprite,
    decode_module,
    mmap_readonly,
    parse_armovie_header,
    read_file_capped,
    transcode_armovie_to_audio,
    transcode_armovie_to_mp4,
)
from ..tools.extraction import convert_fcfs_to_raw
from ..tools.iso9660 import parse_iso9660_pvd
from ..utils.paths import artefact_output_subdir
from ._common import analysis_handler, find_extraction_path, resolve_extraction_file


@analysis_handler("checksum computation", AnalysisType.CHECKSUM_COMPUTE)
def process_checksum_compute(self, analysis: dict, artefact: dict, work_dir: Path):
    """Compute MD5 and SHA256 hashes for the artefact file and store them."""
    analysis_id = analysis['id']
    input_path = self.get_input_path(artefact, work_dir)

    md5, sha256, size = compute_file_hash(input_path)
    self.api.update_artefact_hashes(artefact['uuid'], md5, sha256)

    self.complete_analysis(
        analysis_id,
        summary=f'MD5: {md5}  SHA256: {sha256}',
        details=json.dumps({'md5': md5, 'sha256': sha256, 'size': size}),
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


@analysis_handler("product recognition", AnalysisType.PRODUCT_RECOGNITION)
def process_product_recognition(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process PRODUCT_RECOGNITION analysis.

    Fetches all hash databases that have product recognition enabled, then
    checks the extracted files in each partition of this artefact against
    the product definitions.  A product is matched when all of its required
    files (identified by MD5/SHA1 hash) are present in a single directory.
    Optional files increase the confidence score but are not required.
    When path_match_enabled is set on the product, the file's relative path
    within the matched folder is also checked against the stored relative_path.

    Results are reported back via POST /partitions/<uuid>/recognised-products.
    """
    import json as _json

    analysis_id = analysis['id']
    hints = _json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get(HintKey.PARTITION_UUID)

    if not partition_uuid:
        self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
        return

    # Fetch recognition config (all enabled databases with products)
    config = self.api.get_recognition_config()
    if not config:
        # Nothing to do — no recognition-enabled databases
        self.complete_analysis(analysis_id, summary='No recognition-enabled hash databases configured')
        return

    # Fetch all files in this partition (may be large; paginated internally)
    all_files = self.api.get_partition_files(partition_uuid, show_known='true')

    if not all_files:
        self.complete_analysis(analysis_id, summary='No extracted files in partition')
        return

    # Build index: folder_path -> {hash_set, relative_path_map}
    # folder_path is the parent directory of each file (i.e. path up to last '/')
    # hash_set: set of (md5, sha1) tuples (lowercased)
    # path_map: relative_path_within_folder -> (md5, sha1)
    folder_index: dict[str, dict] = {}
    for f in all_files:
        if f.get('is_directory'):
            continue
        path = f.get('path', '')
        if '/' in path:
            folder = path.rsplit('/', 1)[0]
            rel = path.rsplit('/', 1)[1]
        else:
            folder = ''
            rel = path

        if folder not in folder_index:
            folder_index[folder] = {'hashes': set(), 'path_map': {}}

        md5 = (f.get('md5') or '').lower()
        sha1 = (f.get('sha1') or '').lower()
        if md5 or sha1:
            folder_index[folder]['hashes'].add((md5, sha1))
            folder_index[folder]['path_map'][rel.lower()] = (md5, sha1)

    # Check each product across all databases against each folder
    results = []
    total_products = sum(len(db.get('products', [])) for db in config)

    for db in config:
        for product in db.get('products', []):
            product_id = product['product_id']
            path_match_enabled = product.get('path_match_enabled', False)
            required_files = product.get('required_files', [])
            optional_files = product.get('optional_files', [])

            if not required_files and not optional_files:
                continue

            for folder, idx in folder_index.items():
                folder_hashes = idx['hashes']
                path_map = idx['path_map']

                # Check required files (all must match)
                # When path matching is enabled, relative_path in the product
                # config is the full root-relative path (e.g. '!ArcFS/ArcFS'),
                # but path_map keys are only the filename within the folder
                # (e.g. 'arcfs').  Pre-compute the folder prefix to strip.
                folder_lower = folder.lower()
                folder_prefix = folder_lower + '/' if folder_lower else ''

                required_matched = 0
                for req in required_files:
                    md5 = (req.get('md5') or '').lower()
                    sha1 = (req.get('sha1') or '').lower()
                    rel_path = (req.get('relative_path') or '').lower()

                    matched = False
                    if path_match_enabled and rel_path:
                        # Must match both hash AND relative path.
                        # Strip the folder prefix so '!arcfs/arcfs' becomes
                        # 'arcfs' before looking up in path_map.
                        if folder_prefix and rel_path.startswith(folder_prefix):
                            rel_path_in_folder = rel_path[len(folder_prefix):]
                        else:
                            rel_path_in_folder = rel_path
                        if rel_path_in_folder in path_map:
                            file_md5, file_sha1 = path_map[rel_path_in_folder]
                            matched = (
                                (md5 and file_md5 == md5) or
                                (sha1 and file_sha1 == sha1)
                            )
                    else:
                        # Hash-only match: any file in the folder with this hash
                        matched = any(
                            (md5 and h[0] == md5) or (sha1 and h[1] == sha1)
                            for h in folder_hashes
                        )

                    if matched:
                        required_matched += 1

                if required_files and required_matched < len(required_files):
                    continue  # Not a match — not all required files found

                # Count optional matches
                optional_matched = 0
                for opt in optional_files:
                    md5 = (opt.get('md5') or '').lower()
                    sha1 = (opt.get('sha1') or '').lower()
                    rel_path = (opt.get('relative_path') or '').lower()

                    if path_match_enabled and rel_path:
                        if folder_prefix and rel_path.startswith(folder_prefix):
                            rel_path_in_folder = rel_path[len(folder_prefix):]
                        else:
                            rel_path_in_folder = rel_path
                        if rel_path_in_folder in path_map:
                            file_md5, file_sha1 = path_map[rel_path_in_folder]
                            if (md5 and file_md5 == md5) or (sha1 and file_sha1 == sha1):
                                optional_matched += 1
                    else:
                        if any(
                            (md5 and h[0] == md5) or (sha1 and h[1] == sha1)
                            for h in folder_hashes
                        ):
                            optional_matched += 1

                # For optional-only products, require at least one match
                if not required_files and optional_matched == 0:
                    continue

                results.append({
                    'product_id': product_id,
                    'folder_path': folder if folder else '/',
                    'required_matched': required_matched,
                    'required_total': len(required_files),
                    'optional_matched': optional_matched,
                    'optional_total': len(optional_files),
                })

    self.api.report_recognised_products(partition_uuid, results)

    self.complete_analysis(
        analysis_id,
        summary=f'Checked {total_products} product(s) against {len(folder_index)} folder(s); {len(results)} match(es) found'
    )


@analysis_handler("RISC OS module parse", AnalysisType.RISCOS_MODULE_PARSE)
def process_riscos_module_parse(self, analysis: dict, artefact: dict, work_dir: Path):
    """Parse RISC OS relocatable modules found in an extraction.

    Scans partition files for filetype ffa (Module), reads each from disk,
    and extracts metadata (title, version, date, SWIs, star commands).
    Only queued for Acorn filesystem extractions.
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get(HintKey.PARTITION_UUID)
    extraction_path = hints.get(HintKey.EXTRACTION_PATH)
    path_prefix = hints.get(HintKey.PATH_PREFIX, '')  # set when queued from archive extraction

    if not partition_uuid:
        self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
        return

    # Fetch files with RISC OS filetype ffa (Module).
    # Push the extraction-context filter to the API.
    base_params = {'show_known': 'true'}
    if path_prefix:
        base_params['path_prefix'] = path_prefix
    else:
        base_params['extraction_depth'] = 0

    all_files = self.api.get_partition_files(partition_uuid, **base_params)

    module_files = [
        f for f in all_files
        if (f.get('risc_os_filetype') or '').lower() == 'ffa'
        and not f.get('is_directory', False)
    ]

    if not module_files:
        self.complete_analysis(
            analysis_id,
            summary='No RISC OS modules (filetype ffa) found',
            details=json.dumps({'modules': [], 'files_scanned': 0}),
        )
        return

    # Determine extraction path (same fallback logic as ARCHIVE_EXTRACT)
    if not extraction_path:
        extraction_path = find_extraction_path(self, artefact.get('uuid'))

    if not extraction_path:
        self.fail_analysis(analysis_id, 'Could not determine extraction path')
        return

    modules = []
    parse_errors = 0

    for file_data in module_files:
        db_path = file_data['path']
        risc_os_filetype = file_data.get('risc_os_filetype', '')

        file_path, _disk_path = resolve_extraction_file(
            self, extraction_path, db_path, work_dir,
            path_prefix=path_prefix,
            risc_os_filetype=risc_os_filetype or None,
        )

        if file_path is None:
            log.warning(f"Module file not found on disk: {db_path}")
            parse_errors += 1
            continue

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
        'files_scanned': len(module_files),
        'parse_errors': parse_errors,
    }
    if path_prefix:
        details_dict['path_prefix'] = path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='riscos_module_parser',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )


@analysis_handler("Process Replay file", AnalysisType.REPLAY_PROCESS)
def process_replay(self, analysis: dict, artefact: dict, work_dir: Path):
    """Process Acorn Replay / ARMovie files found in an extraction.

    Scans partition files for filetype ae7 (ARMovie), reads each from disk, and
    parses the text header + chunk catalogue into searchable metadata.  Named
    "process" (not "parse") because this handler will later be extended to
    transcode the video to a portable format.

    Only meaningful for extractions that contain ARMovie files; a harmless
    no-op otherwise.
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get(HintKey.PARTITION_UUID)
    extraction_path = hints.get(HintKey.EXTRACTION_PATH)
    path_prefix = hints.get(HintKey.PATH_PREFIX, '')  # set when queued from archive extraction

    if not partition_uuid:
        self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
        return

    # Fetch files with RISC OS filetype ae7 (ARMovie).
    base_params = {'show_known': 'true'}
    if path_prefix:
        base_params['path_prefix'] = path_prefix
    else:
        base_params['extraction_depth'] = 0

    all_files = self.api.get_partition_files(partition_uuid, **base_params)

    replay_files = [
        f for f in all_files
        if (f.get('risc_os_filetype') or '').lower() == 'ae7'
        and not f.get('is_directory', False)
    ]

    if not replay_files:
        self.complete_analysis(
            analysis_id,
            summary='No Acorn Replay / ARMovie files (filetype ae7) found',
            details=json.dumps({'movies': [], 'files_scanned': 0}),
        )
        return

    if not extraction_path:
        extraction_path = find_extraction_path(self, artefact.get('uuid'))

    if not extraction_path:
        self.fail_analysis(analysis_id, 'Could not determine extraction path')
        return

    movies = []
    parse_errors = 0

    for file_data in replay_files:
        db_path = file_data['path']
        risc_os_filetype = file_data.get('risc_os_filetype', '')

        file_path, _disk_path = resolve_extraction_file(
            self, extraction_path, db_path, work_dir,
            path_prefix=path_prefix,
            risc_os_filetype=risc_os_filetype or None,
        )

        if file_path is None:
            log.warning(f"ARMovie file not found on disk: {db_path}")
            parse_errors += 1
            continue

        try:
            with mmap_readonly(file_path) as data:
                result = parse_armovie_header(data)
            result['file_path'] = db_path
            movies.append(result)
        except ArmovieParseError as e:
            log.warning(f"Could not parse ARMovie {db_path}: {e}")
            parse_errors += 1
        except Exception as e:
            log.warning(f"Unexpected error parsing ARMovie {db_path}: {e}")
            parse_errors += 1

    summary_parts = [f'Parsed {len(movies)} Acorn Replay / ARMovie file(s)']
    if parse_errors:
        summary_parts.append(f'{parse_errors} could not be parsed')

    details_dict: dict = {
        'movies': movies,
        'files_scanned': len(replay_files),
        'parse_errors': parse_errors,
    }
    if path_prefix:
        details_dict['path_prefix'] = path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='armovie_parser',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )

    # Queue the transcode follow-up only when ARMovie files were actually found
    # and indexed.  This runs *after* complete_analysis so the ReplayMovie rows
    # exist before REPLAY_TRANSCODE's result tries to update them with the MP4
    # path (see handle_replay_transcode in the web search-index service).
    if movies:
        transcode_hints = {HintKey.PARTITION_UUID: partition_uuid}
        if extraction_path:
            transcode_hints[HintKey.EXTRACTION_PATH] = extraction_path
        if path_prefix:
            transcode_hints[HintKey.PATH_PREFIX] = path_prefix
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.REPLAY_TRANSCODE.value,
            hints=transcode_hints,
        )


def _save_replay_poster(worker, file_path, header, work_dir, output_subdir, base_name):
    """Extract an ARMovie embedded poster sprite to PNG and save it as output.

    Returns the saved output-file path (relative, as stored), or None when the
    movie has no embedded poster sprite or extraction failed.  Best-effort: a
    poster is a nicety, never a reason to fail the transcode.

    The movie file is memory-mapped so only the sprite region is faulted in —
    a multi-GB Replay file is never loaded into RAM to grab its thumbnail.
    """
    sprite_offset = header.get('sprite_offset')
    sprite_size = header.get('sprite_size')
    if not sprite_size or sprite_size <= 0 or sprite_offset is None:
        return None

    poster_name = f'{base_name}_poster.png'
    poster_path = work_dir / poster_name
    with mmap_readonly(file_path) as data:
        result = convert_replay_poster_sprite(data, sprite_offset, sprite_size, poster_path)
    if not result.get('success'):
        log.info("Replay poster sprite extraction failed for %s: %s", base_name, result.get('error'))
        return None
    return worker.save_output_file(poster_path, poster_name, subdir=output_subdir)


@analysis_handler("Transcode Replay video", AnalysisType.REPLAY_TRANSCODE)
def process_replay_transcode(self, analysis: dict, artefact: dict, work_dir: Path):
    """Transcode Acorn Replay / ARMovie files found in an extraction to MP4.

    Mirrors :func:`process_replay`'s ae7 file discovery, then for each movie
    runs scotch's ``replay-transcode`` (decode to raw RGB24 + WAV) piped into
    ffmpeg (mux to H.264/AAC MP4) and grabs a first-frame poster thumbnail.
    The MP4 and poster are saved as analysis output files and reported so the
    web side can attach them to the matching ReplayMovie row.

    Transcoding is best-effort per file: a movie whose codec needs a RISC OS
    decompressor module that is not available is recorded as an error and
    skipped, leaving its parsed metadata intact.
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get(HintKey.PARTITION_UUID)
    extraction_path = hints.get(HintKey.EXTRACTION_PATH)
    path_prefix = hints.get(HintKey.PATH_PREFIX, '')

    if not partition_uuid:
        self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
        return

    base_params = {'show_known': 'true'}
    if path_prefix:
        base_params['path_prefix'] = path_prefix
    else:
        base_params['extraction_depth'] = 0

    all_files = self.api.get_partition_files(partition_uuid, **base_params)

    replay_files = [
        f for f in all_files
        if (f.get('risc_os_filetype') or '').lower() == 'ae7'
        and not f.get('is_directory', False)
    ]

    if not replay_files:
        self.complete_analysis(
            analysis_id,
            summary='No Acorn Replay / ARMovie files (filetype ae7) found',
            details=json.dumps({'transcoded': [], 'files_scanned': 0}),
        )
        return

    if not extraction_path:
        extraction_path = find_extraction_path(self, artefact.get('uuid'))

    if not extraction_path:
        self.fail_analysis(analysis_id, 'Could not determine extraction path')
        return

    output_subdir = artefact_output_subdir(artefact)
    # Only pass --modules-dir when it actually exists (it won't when the worker
    # runs outside the Docker image that bundles the codecs).
    modules_dir = REPLAY_MODULES_DIR if REPLAY_MODULES_DIR and Path(REPLAY_MODULES_DIR).is_dir() else None

    transcoded = []
    transcode_errors = []

    for index, file_data in enumerate(replay_files):
        db_path = file_data['path']
        risc_os_filetype = file_data.get('risc_os_filetype', '')

        file_path, _disk_path = resolve_extraction_file(
            self, extraction_path, db_path, work_dir,
            path_prefix=path_prefix,
            risc_os_filetype=risc_os_filetype or None,
        )

        if file_path is None:
            log.warning(f"ARMovie file not found on disk: {db_path}")
            transcode_errors.append({'file_path': db_path, 'error': 'File not found on disk'})
            continue

        # Need the frame geometry from the header to drive ffmpeg's rawvideo
        # input; the raw bytes are also reused for the embedded poster sprite.
        try:
            with mmap_readonly(file_path) as data:
                header = parse_armovie_header(data)
        except ArmovieParseError as e:
            transcode_errors.append({'file_path': db_path, 'error': f'Header parse failed: {e}'})
            continue

        base_name = f'{analysis["uuid"]}_{index}'

        if header.get('video_format') == 0:
            # Sound-only movie — no video frames, but still playable as audio.
            # Many sound-only Replay files carry a poster sprite (a title card);
            # extract it so the audio player and grid have a thumbnail.
            audio_name = f'{base_name}.m4a'
            audio_path = work_dir / audio_name
            result = transcode_armovie_to_audio(
                file_path, audio_path,
                work_dir=work_dir,
                modules_dir=modules_dir,
            )
            if not result['success']:
                log.warning(f"Audio transcode failed for {db_path}: {result.get('error')}")
                transcode_errors.append({
                    'file_path': db_path,
                    'error': result.get('error', 'Audio transcode failed'),
                    'stage': result.get('stage'),
                })
                continue
            saved_audio = self.save_output_file(audio_path, audio_name, subdir=output_subdir)
            saved_poster = _save_replay_poster(self, file_path, header, work_dir, output_subdir, base_name)
            transcoded.append({
                'file_path': db_path,
                'mp4_output_path': saved_audio,   # media output (audio for sound-only)
                'poster_path': saved_poster,
                'has_audio': True,
                'audio_only': True,
                'width': None,
                'height': None,
                'tool_result': result,
            })
            continue

        mp4_name = f'{base_name}.mp4'
        frame_name = f'{base_name}.jpg'
        mp4_path = work_dir / mp4_name
        frame_path = work_dir / frame_name

        # Prefer the author-supplied embedded poster sprite as the thumbnail; only
        # fall back to ffmpeg's first decoded frame when there is no poster sprite.
        saved_poster = _save_replay_poster(self, file_path, header, work_dir, output_subdir, base_name)

        result = transcode_armovie_to_mp4(
            file_path, mp4_path,
            width=header.get('width'),
            height=header.get('height'),
            frame_rate=header.get('frame_rate'),
            work_dir=work_dir,
            modules_dir=modules_dir,
            poster_path=None if saved_poster else frame_path,
        )

        if not result['success']:
            log.warning(f"Transcode failed for {db_path}: {result.get('error')}")
            transcode_errors.append({
                'file_path': db_path,
                'error': result.get('error', 'Transcode failed'),
                'stage': result.get('stage'),
            })
            continue

        saved_mp4 = self.save_output_file(mp4_path, mp4_name, subdir=output_subdir)
        if not saved_poster and result.get('poster_path'):
            saved_poster = self.save_output_file(frame_path, frame_name, subdir=output_subdir)

        transcoded.append({
            'file_path': db_path,
            'mp4_output_path': saved_mp4,
            'poster_path': saved_poster,
            'has_audio': result.get('has_audio', False),
            'width': result.get('width'),
            'height': result.get('height'),
            'tool_result': result,
        })

    summary_parts = [f'Transcoded {len(transcoded)} ARMovie file(s) to MP4']
    if transcode_errors:
        summary_parts.append(f'{len(transcode_errors)} could not be transcoded')

    details_dict: dict = {
        'transcoded': transcoded,
        'transcode_errors': transcode_errors,
        'files_scanned': len(replay_files),
    }
    if path_prefix:
        details_dict['path_prefix'] = path_prefix

    self.complete_analysis(
        analysis_id,
        tool_name='replay-transcode,ffmpeg',
        summary=', '.join(summary_parts),
        details=json.dumps(details_dict),
    )
# vim: ts=4 sw=4 et
