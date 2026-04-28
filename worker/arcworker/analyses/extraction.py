"""
File / archive extraction analysis handlers.

Covers FILE_EXTRACTION (disc/sector image → file tree), ARCHIVE_DETECT
(scan a partition for nested archives), and ARCHIVE_EXTRACT (extract a
detected archive — at top level or nested).
"""

import json
import shutil
from pathlib import Path

from shared.enums import AnalysisType, ArtefactType

from ..config import log
from ..tools import (
    decompress_single_file,
    detect_fat_filesystem,
    enumerate_extracted_files,
    extract_7z,
    extract_acorn_disc_image_manager,
    extract_dos_7z,
    extract_rar,
    extract_riscosarc,
    extract_tar,
    extract_tbafs,
    extract_xfiles,
    extract_zip,
    extract_zip_riscos,
    has_riscos_zip_metadata,
    parse_iso_riscos_filetypes,
    read_fat_volume_label,
)
from ..tools.extraction import _parse_dim_report, convert_fcfs_to_raw
from ..tools.iso9660 import parse_iso9660_pvd
from ._common import analysis_handler


def _apply_pling_renames(extract_dir: Path, rename_map: dict[str, str]) -> None:
    """
    Rename ISO 9660 pling-mapped entries in the extraction directory so that
    physical filenames match the pling-corrected DB paths.

    ISO 9660 forbids '!' so Acorn mastering tools store application
    directories (and occasionally files) as '_NAME'.  This function renames
    them to '!NAME' so that subsequent lookups (module parser, archive
    extraction, FORMAT_CONVERT) can find files using their DB paths directly.

    ``rename_map`` maps lowercase raw ISO 9660 paths to pling-corrected
    display paths (e.g. ``'_arcfs/arcfs'`` → ``'!ARCFS/ARCFS'``).
    The function derives the set of unique directory renames from these
    entries and applies them shallowest-first so that parent renames happen
    before any child entries are processed.
    """
    # Collect directory renames: src_rel → dst_rel.
    # For each file path in rename_map, walk the components and identify
    # every component where '_' was replaced with '!' (a pling entry).
    dir_renames: dict[str, str] = {}

    for _raw_lower, display_path in rename_map.items():
        display_parts = display_path.split('/')
        for i, dp in enumerate(display_parts[:-1]):  # skip the filename itself
            if not dp.startswith('!'):
                continue
            # This directory component has a pling.  Reconstruct its on-disk
            # name: '_' + everything after '!' in the display name.  ISO 9660
            # directory names are uppercase and the display name preserves that
            # case, so '_' + dp[1:] gives the exact on-disk name.
            raw_component = '_' + dp[1:]
            # Build the src path using the already-pling-corrected parent
            # components (since we process shallowest first, parents are renamed
            # before we need to reference them in deeper entries).
            src_rel = '/'.join(display_parts[:i] + [raw_component])
            dst_rel = '/'.join(display_parts[:i + 1])
            dir_renames[src_rel] = dst_rel

    # Also handle pling on the filename itself (unusual but possible).
    for _raw_lower, display_path in rename_map.items():
        display_parts = display_path.split('/')
        fname = display_parts[-1]
        if fname.startswith('!'):
            raw_fname = '_' + fname[1:]
            src_rel = '/'.join(display_parts[:-1] + [raw_fname])
            dst_rel = display_path
            dir_renames[src_rel] = dst_rel

    # Sort by depth (shallowest first) so parent renames precede child renames.
    for src_rel, dst_rel in sorted(dir_renames.items(), key=lambda x: x[0].count('/')):
        src = extract_dir / src_rel
        dst = extract_dir / dst_rel
        if src.exists() and not dst.exists():
            try:
                src.rename(dst)
                log.debug(f"Pling rename: {src_rel!r} → {dst_rel!r}")
            except OSError as e:
                log.warning(f"Could not pling-rename {src_rel!r} to {dst_rel!r}: {e}")


# Extracted files with these extensions are promoted to derived artefacts
# so they get their own analysis pipeline (e.g. an ISO inside a ZIP gets
# FILE_EXTRACTION queued automatically).  Keep in sync with EXTENSION_MAP
# in myapp/blueprints/artefacts.py.  Bound to AnalysisWorker as a class
# attribute so handler bodies can reference self._PROMOTABLE_EXTENSIONS.
_PROMOTABLE_EXTENSIONS = {
    '.scp': ArtefactType.SCP,
    '.dfi': ArtefactType.DFI,
    '.a2r': ArtefactType.A2R,
    '.imd': ArtefactType.IMD,
    '.hfe': ArtefactType.HFE,
    '.adf': ArtefactType.RAW_SECTOR,
    '.img': ArtefactType.RAW_SECTOR,
    '.ima': ArtefactType.RAW_SECTOR,
    '.dsk': ArtefactType.RAW_SECTOR,
    '.dd':  ArtefactType.RAW_SECTOR,
    '.iso': ArtefactType.ISO,
}


def _sniff_archive_magic(file_path: Path):
    """Sniff the first bytes of a file to detect mis-labelled archives.

    Returns the detected ArchiveType, or ``None`` when the format is
    unrecognised.  Used both for top-level artefacts (ZIP that is
    really Spark) and nested archives (``&DDC`` file that is really
    ZIP).

    Recognised signatures:
      ArcFS: ``Archive\\0`` or ``\\x1Aarchive``
      Spark: ``\\x1A`` followed by ``\\x00``, ``\\x80``–``\\x89``, or ``\\xFF``
      ZIP:   ``PK\\x03\\x04``
    """
    from shared.archive_formats import ArchiveType

    try:
        with open(file_path, 'rb') as fh:
            header = fh.read(8)
    except OSError:
        return None

    if len(header) < 2:
        return None

    # ArcFS: "Archive\0" or "\x1aarchive"
    if header[:8] == b'Archive\x00':
        return ArchiveType.ARCFS
    if len(header) >= 8 and header[0] == 0x1A and header[1:8] == b'archive':
        return ArchiveType.ARCFS

    # Spark: 0x1A followed by 0x00, 0x80-0x89, or 0xFF
    if header[0] == 0x1A:
        second = header[1]
        if second == 0x00 or (0x80 <= second <= 0x89) or second == 0xFF:
            return ArchiveType.SPARK

    # ZIP: PK\x03\x04
    if len(header) >= 4 and header[:4] == b'PK\x03\x04':
        return ArchiveType.ZIP

    # TBAFS: "TAFS" followed by 0xC8
    if len(header) >= 5 and header[:4] == b'TAFS' and header[4] == 0xC8:
        return ArchiveType.TBAFS

    # X-Files: "XFIL" magic at offset 0
    if len(header) >= 4 and header[:4] == b'XFIL':
        return ArchiveType.XFILES

    return None


def _is_riscos_zip(file_path: Path) -> bool:
    """Check whether a ZIP archive contains RISC OS metadata.

    Delegates to :func:`has_riscos_zip_metadata` which scans the
    central directory extra fields for the Acorn/SparkFS header ID
    (0x4341).  Used to upgrade ``ArchiveType.ZIP`` to ``ZIP_RISCOS``
    for plain ``.zip`` uploads that have no RISC OS filetype metadata.
    """
    return has_riscos_zip_metadata(file_path)


def _extract_top_level_archive(
    self, analysis, artefact, work_dir,
    archive_type, archive_info,
    extract_zip, extract_tar, extract_rar, extract_7z,
):
    """Handle ARCHIVE_EXTRACT for a top-level artefact (no partition).

    Extracts the artefact file directly, creates a partition for the
    extracted files, queues follow-on analyses, and promotes any
    recognised disc images to derived artefacts.
    """
    import json

    from shared.archive_formats import ArchiveType, get_archive_info

    from ..config import OUTPUT_DIR
    from ..utils.paths import get_output_path

    analysis_id = analysis['id']
    item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

    extract_dir = get_output_path(
        OUTPUT_DIR, item, artefact, analysis, partition=None
    )
    input_path = self.get_input_path(artefact, work_dir)

    # get_input_path() runs decompress_if_needed() which may have already
    # stripped the outer compression wrapper (e.g. .tar.gz → .tar), or
    # the file may be mis-labelled (named .tar.gz but actually plain tar).
    # In either case, downgrade to plain TAR so extract_tar() doesn't try
    # to decompress again.  This is safe because:
    #   - If decompression succeeded, the file is now a plain tar.
    #   - If decompression was skipped (not actually compressed), the file
    #     was already a plain tar despite the extension.
    _COMPRESSED_TAR_TYPES = {
        ArchiveType.TARGZ: ArchiveType.TAR,
        ArchiveType.TARBZ2: ArchiveType.TAR,
        ArchiveType.TARXZ: ArchiveType.TAR,
    }
    if archive_type in _COMPRESSED_TAR_TYPES:
        old_type = archive_type
        archive_type = _COMPRESSED_TAR_TYPES[archive_type]
        archive_info = get_archive_info(archive_type)
        log.info(f"Post-decompression: using {archive_type.value} (was {old_type.value})")

    # Sniff magic bytes — some RISC OS archives are distributed with
    # a .zip extension even though they are actually Spark or ArcFS.
    sniffed = self._sniff_archive_magic(input_path)
    if sniffed is not None and sniffed != archive_type:
        log.info(f"Magic-byte sniff overrides {archive_type.value} → {sniffed.value}")
        # A ZIP found via RISC OS filetype (including zip_riscos itself)
        # should be treated as ZIP_RISCOS so Acorn ,xxx suffixes are
        # parsed correctly and the container format is recorded accurately.
        _RISCOS_TYPES = (
            ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.PACKDIR,
            ArchiveType.TBAFS, ArchiveType.CFS, ArchiveType.SQUASH,
            ArchiveType.ZIP_RISCOS,
        )
        if sniffed == ArchiveType.ZIP and archive_type in _RISCOS_TYPES:
            sniffed = ArchiveType.ZIP_RISCOS
        archive_type = sniffed
        archive_info = get_archive_info(archive_type)

    # A plain .zip upload whose contents have RISC OS ,xxx filetype
    # suffixes should be treated as ZIP_RISCOS so the CP437→RISC OS
    # Latin-1 filename fix runs during extraction.
    if archive_type == ArchiveType.ZIP and self._is_riscos_zip(input_path):
        log.info("ZIP contains Acorn extra-field (0x4341) metadata — upgrading to ZIP_RISCOS")
        archive_type = ArchiveType.ZIP_RISCOS
        archive_info = get_archive_info(archive_type)

    # Dispatch to the correct extraction tool
    from functools import partial

    _dispatch = {
        ArchiveType.SPARK:      extract_riscosarc,
        ArchiveType.ARCFS:      extract_riscosarc,
        ArchiveType.PACKDIR:    extract_riscosarc,
        ArchiveType.CFS:        extract_riscosarc,
        ArchiveType.SQUASH:     extract_riscosarc,
        ArchiveType.TBAFS:      extract_tbafs,
        ArchiveType.XFILES:     extract_xfiles,
        ArchiveType.ZIP_RISCOS: extract_zip_riscos,
        ArchiveType.ZIP:        extract_zip,
        ArchiveType.TAR:        partial(extract_tar, archive_type=ArchiveType.TAR.value),
        ArchiveType.TARGZ:      partial(extract_tar, archive_type=ArchiveType.TARGZ.value),
        ArchiveType.TARBZ2:     partial(extract_tar, archive_type=ArchiveType.TARBZ2.value),
        ArchiveType.TARXZ:      partial(extract_tar, archive_type=ArchiveType.TARXZ.value),
        ArchiveType.RAR:        extract_rar,
        ArchiveType.SEVENZ:     extract_7z,
    }

    extractor = _dispatch.get(archive_type)
    if extractor is None:
        self.fail_analysis(
            analysis_id,
            f'Top-level extraction not supported for archive type: {archive_type.value}'
        )
        return

    result = extractor(input_path, extract_dir)

    # Spark/ArcFS fallback: if riscosarc fails, the file might
    # actually be a ZIP with RISC OS filetypes (SparkFS uses
    # filetype &DDC for both Spark and ZIP).
    if not result['success'] and archive_type == ArchiveType.SPARK:
        result = extract_zip_riscos(input_path, extract_dir)
        if result['success']:
            archive_type = ArchiveType.ZIP_RISCOS
            archive_info = get_archive_info(archive_type)

    if not result['success']:
        shutil.rmtree(extract_dir, ignore_errors=True)
        self.fail_analysis(
            analysis_id,
            result.get('error', 'Archive extraction failed'),
            tool_name=result.get('tool'),
            details=json.dumps({'process_output': result.get('process_output')}),
        )
        return

    files = enumerate_extracted_files(
        extract_dir, acorn='auto',
        inf_metadata=result.get('inf_metadata'),
    )

    partition = self.api.register_file_listing(
        artefact['uuid'], files, 'archive',
        container_format=archive_info['name'],
    )

    # Promote extracted files with recognised extensions to derived
    # artefacts so they get their own analysis pipeline.
    derived_count = 0
    for file_path in extract_dir.rglob('*'):
        if not file_path.is_file():
            continue
        ext = file_path.suffix.lower()
        artefact_type = self._PROMOTABLE_EXTENSIONS.get(ext)
        if artefact_type is None:
            continue
        resp = self.api.register_derived_artefact(
            analysis_id,
            label=file_path.name,
            source_path=file_path,
            artefact_type=artefact_type,
        )
        if resp:
            derived_count += 1
            log.info(f"Promoted {file_path.name} to derived {artefact_type.value} artefact")

    # Upload extraction tree to storage (no-op in local mode)
    self._upload_extraction_tree(extract_dir)
    rel_output_path = self._relative_output_path(extract_dir)

    self.complete_analysis(
        analysis_id,
        tool_name=result['tool'],
        output_path=rel_output_path,
        summary=f"Extracted {len(files)} files from {archive_info['name']}"
                + (f" ({derived_count} promoted to artefacts)" if derived_count else ""),
        details=json.dumps({
            'file_count': len(files),
            'archive_type': archive_type.value,
            'derived_artefacts': derived_count,
        }),
    )

    if partition:
        self.queue_partition_follow_ups(
            artefact['uuid'],
            partition.get('uuid'),
            extraction_path=rel_output_path,
        )


@analysis_handler("file extraction")
def process_file_extraction(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process FILE_EXTRACTION analysis.
    Extracts files from a disc/sector image to persistent storage,
    registers the file listing in the database, and queues archive detection.
    Only works on raw sector images (IMG) - not HFE or IMD formats.

    When a 'partition_image_path' hint is provided (set by PARTITION_DETECT),
    uses that file directly instead of re-decompressing the original artefact.
    """
    from ..config import OUTPUT_DIR
    from ..utils.paths import get_output_path

    analysis_id = analysis['id']
    artefact_type = artefact.get('artefact_type', '')

    # Only raw sector images can be processed by 7z and DIM
    # HFE is an emulator container format, IMD is track-based with metadata
    # These need to be converted to IMG first via flux_decode
    supported_types = (
        ArtefactType.ISO.value,
        ArtefactType.RAW_SECTOR.value,
        ArtefactType.DD_ZST.value,
        ArtefactType.DD_GZ.value,
        ArtefactType.DD_BZ2.value,
    )
    if artefact_type not in supported_types:
        self.fail_analysis(
            analysis_id,
            f'File extraction not supported for {artefact_type} format. Only raw sector images are supported.',
            details=json.dumps({
                'artefact_type': artefact_type,
                'supported_types': list(supported_types),
            })
        )
        return

    hints = json.loads(analysis.get('hints') or '{}')
    filesystem = hints.get('filesystem', '').lower()
    partition_index = hints.get('partition_index', 0)
    hint_container_format = hints.get('container_format', '')

    # Use cached partition image from PARTITION_DETECT when available,
    # avoiding redundant decompression of the original artefact.
    input_path = self._resolve_partition_image(
        hints.get('partition_image_path'), artefact, work_dir)

    # Get Item for hierarchical path
    item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

    # Use hierarchical output path for persistent storage
    extract_dir = get_output_path(
        OUTPUT_DIR,
        item,
        artefact,
        analysis,
        partition=None
    )

    # Track every tool attempted so all process_output ends up in details.
    all_results: dict[str, dict] = {}

    # Determine filesystem type.
    # Treat 'unknown' the same as an absent hint so that a successful DIM
    # run can upgrade the filesystem type (fixes the case where
    # PARTITION_DETECT couldn't identify the format but DIM can).
    if filesystem and filesystem != 'unknown':
        fs_type = filesystem
    elif artefact_type == ArtefactType.ISO.value:
        fs_type = 'iso9660'
    elif hint_container_format:
        container_lower = hint_container_format.lower()
        if 'adfs' in container_lower:
            fs_type = 'adfs'
        elif 'dfs' in container_lower:
            fs_type = 'dfs'
        elif 'acorn' in container_lower:
            fs_type = 'adfs'
        else:
            fs_type = 'unknown'
    else:
        fs_type = 'unknown'

    # Choose extraction method based on filesystem hint
    is_acorn = False
    log.info(f"file_extraction: FS is '{fs_type}' on a {artefact_type} artefact")
    if fs_type in ('dfs', 'adfs', 'acorn'):
        result = extract_acorn_disc_image_manager(input_path, extract_dir)
        all_results['dim'] = result
        is_acorn = True
    elif fs_type in ('fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos', 'iso9660'):
        result = extract_dos_7z(input_path, extract_dir)
        all_results['7z'] = result
    else:
        log.info(f"CHECKPOINT: FS is '{fs_type}', I dunno, falling back!")
        # No filesystem hint — read the boot-sector BPB first.  If the
        # image is FAT12/16/32, skip DIM entirely and go straight to 7z:
        # DIM can read DOS FAT images but produces double-extension names.
        # We must NOT try 7z first without this check because 7z will
        # "succeed" on ADFS images containing ZIP files by extracting the
        # embedded ZIP rather than the actual disc filesystem.
        if detect_fat_filesystem(input_path):
            result = extract_dos_7z(input_path, extract_dir)
            all_results['7z'] = result
        else:
            result = extract_acorn_disc_image_manager(input_path, extract_dir)
            all_results['dim'] = result
            if result['success']:
                is_acorn = True
            else:
                result = extract_dos_7z(input_path, extract_dir)
                all_results['7z'] = result

    def _build_details(extra: dict | None = None) -> str:
        d: dict = {}
        if extra:
            d.update(extra)
        for tool_key, tool_result in all_results.items():
            po = tool_result.get('process_output')
            if po:
                d[tool_key] = {'process_output': po}
        return json.dumps(d)

    if not result['success']:
        shutil.rmtree(extract_dir, ignore_errors=True)
        self.fail_analysis(
            analysis_id,
            result.get('error', 'Extraction failed'),
            tool_name=result.get('tool'),
            details=_build_details()
        )
        return

    # For ISO 9660 artefacts: parse the ARCHIMEDES extension to obtain
    # per-file RISC OS filetypes from load/exec addresses.  Also enable
    # acorn='auto' so that any files whose names already carry a ',xxx'
    # suffix (e.g. from Rock Ridge NM entries preserved by 7z) are handled
    # by the existing suffix-parsing logic.
    iso_filetype_map: dict[str, str] = {}
    if artefact_type == ArtefactType.ISO.value:
        iso_filetype_map, iso_rename_map = parse_iso_riscos_filetypes(input_path)
        log.info(
            f"ISO ARCHIMEDES parser found {len(iso_filetype_map)} filetype entries, "
            f"{len(iso_rename_map)} pling renames"
        )
        # Rename '_NAME' directories/files to '!NAME' on disk so that
        # physical paths match the pling-corrected paths stored in the DB.
        # This lets the module parser, archive extractor, and FORMAT_CONVERT
        # locate files directly without any reverse-lookup logic.
        if iso_rename_map:
            _apply_pling_renames(extract_dir, iso_rename_map)

    # DIM processes INF sidecar files during extraction and returns
    # the collected metadata.  For non-DIM paths (DOS/ISO), no INFs
    # are produced so the dict is empty.
    inf_metadata = result.get('inf_metadata', {})

    # Enumerate extracted files to build file listing.
    # ISO artefacts use acorn='auto' to catch ',xxx' suffix filenames;
    # Acorn disc images (is_acorn=True) always parse the suffix.
    acorn_mode: bool | str
    if artefact_type == ArtefactType.ISO.value:
        acorn_mode = 'auto'
    else:
        acorn_mode = is_acorn
    files = enumerate_extracted_files(
        extract_dir,
        acorn=acorn_mode,
        filetype_map=iso_filetype_map,
        inf_metadata=inf_metadata,
    )

    # Write ISO metadata sidecar AFTER enumerate_extracted_files so it is
    # not included in the file listing.  FORMAT_CONVERT reads this to detect
    # viewable types without re-parsing the ISO image.
    if iso_filetype_map:
        import json as _json
        sidecar_path = extract_dir / '_arcology_iso_meta.json'
        try:
            with open(sidecar_path, 'w', encoding='utf-8') as _sf:
                _json.dump({'filetype_map': iso_filetype_map}, _sf)
        except OSError as _e:
            log.warning(f"Could not write ISO metadata sidecar: {_e}")

    # Extract disc metadata from DIM report output (if Acorn)
    disc_name = None
    container_format = None
    if is_acorn and result.get('process_output'):
        metadata = _parse_dim_report(result['process_output'].get('stdout', ''))
        disc_name = metadata.get('disc_name')
        container_format = metadata.get('container_format')

    # For DOS/FAT images, read the volume label straight from the boot
    # sector / root directory.  7z does not surface this information, so
    # the label would otherwise be lost.
    _FAT_FS_TYPES = {'fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos'}
    if disc_name is None and fs_type in _FAT_FS_TYPES:
        try:
            disc_name = read_fat_volume_label(input_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(f"FAT volume label read failed: {exc}")
            disc_name = None

    # For ISO 9660 images, use the volume identifier from the Primary
    # Volume Descriptor as the partition label.
    if disc_name is None and fs_type == 'iso9660':
        disc_name = parse_iso9660_pvd(input_path).get('volume_identifier')

    # When DIM reports a generic format ("Acorn ADFS Hard Disc") but
    # PARTITION_DETECT already identified a specific subformat (e.g.
    # "Acorn ADFS F"), prefer the more specific hint.  Also use the hint
    # when DIM produced no container_format at all.
    if hint_container_format and (
        not container_format
        or 'hard disc' in container_format.lower()
    ):
        container_format = hint_container_format

    # If fs_type is still 'unknown' but DIM identified the format via
    # container_format, upgrade fs_type now.  This handles the case where
    # PARTITION_DETECT could not identify the disc (fell back to 'unknown')
    # but DIM succeeded and reported e.g. "Acorn ADFS E".
    if fs_type == 'unknown' and container_format:
        _cf_lower = container_format.lower()
        if 'adfs' in _cf_lower:
            fs_type = 'adfs'
        elif 'dfs' in _cf_lower:
            fs_type = 'dfs'

    # For DOS/FAT filesystems processed by 7z, construct a human-readable
    # container_format so the UI hover tooltip is populated.  DIM sets this
    # automatically for Acorn images; for DOS images DIM is never used.
    if not container_format:
        _iso_and_fat_labels = {
            'iso9660': 'ISO 9660',
            'fat12': 'DOS FAT12',
            'fat16': 'DOS FAT16',
            'fat32': 'DOS FAT32',
            'fat':   'DOS FAT',
            'dos':   'DOS',
            'msdos': 'MS-DOS',
        }
        container_format = _iso_and_fat_labels.get(fs_type)

    # Register partition and file listing in the database
    partition = self.api.register_file_listing(
        artefact['uuid'],
        files,
        fs_type,
        label=disc_name,
        container_format=container_format,
        partition_index=partition_index,
    )

    # Upload extraction tree to storage (no-op in local mode)
    self._upload_extraction_tree(extract_dir)
    rel_output_path = self._relative_output_path(extract_dir)

    self.complete_analysis(
        analysis_id,
        tool_name=result['tool'],
        summary=f'Extracted {len(files)} files ({fs_type})',
        output_path=rel_output_path,
        details=_build_details({'file_count': len(files)})
    )

    # Queue ARCHIVE_DETECT to scan extracted files for nested archives
    if partition:
        self.queue_partition_follow_ups(
            artefact['uuid'],
            partition.get('uuid'),
            extraction_path=rel_output_path,
        )


@analysis_handler("archive detection")
def process_archive_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process ARCHIVE_DETECT analysis.
    Scans partition files for archives and queues extraction jobs.
    """
    import json

    from shared.archive_formats import (
        get_archive_by_extension,
        get_archive_by_filetype,
        get_archive_info,
        is_compressor_format,
    )

    from ..config import MAX_ARCHIVE_DEPTH

    analysis_id = analysis['id']

    hints = json.loads(analysis.get('hints') or '{}')
    partition_uuid = hints.get('partition_uuid')
    extraction_path = hints.get('extraction_path')
    path_prefix = hints.get('path_prefix', '')

    if not partition_uuid:
        self.fail_analysis(analysis_id, 'No partition_uuid in analysis hints')
        return

    # Get files not yet marked as archives (skip already-detected ones).
    # Must include known files (show_known=true) because archive files
    # can match the known-files database and would otherwise be hidden.
    partition_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000&is_archive=false&show_known=true")
    if not partition_resp:
        self.fail_analysis(analysis_id, 'Failed to get partition files')
        return

    files = partition_resp.get('files', [])

    # Filter files to only those belonging to this archive's extraction
    # context.  Without this, nested ARCHIVE_DETECT jobs pick up files
    # from unrelated archives in the same partition and pass them wrong
    # extraction_path / path_prefix hints, causing "file not found" in
    # the subsequent ARCHIVE_EXTRACT.
    if path_prefix:
        # Nested detection: only process files extracted from this archive
        # (their DB paths are prefixed with the archive's own path).
        files = [f for f in files if f.get('path', '').startswith(path_prefix + '/')]
    else:
        # Top-level detection (after FILE_EXTRACTION): only process files
        # that came directly from the disc image, not from nested archives.
        files = [f for f in files if f.get('extraction_depth', 0) == 0]

    archive_count = 0
    queued_count = 0
    depth_limit_exceeded = 0
    compressor_count = 0

    for file_data in files:
        filetype = file_data.get('risc_os_filetype')
        filename = file_data.get('filename', '')

        # Try detecting by RISC OS filetype first
        archive_type = get_archive_by_filetype(filetype) if filetype else None

        # Fall back to extension-based detection (for PC archives)
        if not archive_type:
            archive_type = get_archive_by_extension(filename)

        if not archive_type:
            continue

        archive_info = get_archive_info(archive_type)

        # Check if this is a single-file compressor
        is_compressor = is_compressor_format(archive_type)

        # Check depth limit
        current_depth = file_data.get('extraction_depth', 0)
        if current_depth >= MAX_ARCHIVE_DEPTH:
            depth_limit_exceeded += 1
            # Mark as archive but don't queue extraction
            self.api.post(f"/files/{file_data['id']}/mark_archive", {
                'is_archive': True,
                'archive_format': archive_info['name']
            })
            continue

        # Mark as archive
        self.api.post(f"/files/{file_data['id']}/mark_archive", {
            'is_archive': True,
            'archive_format': archive_info['name']
        })
        archive_count += 1
        if is_compressor:
            compressor_count += 1

        # Queue extraction
        extract_hints = {
            'file_id': file_data['id'],
            'partition_uuid': partition_uuid,
            'archive_type': archive_type.value,
            'archive_format': archive_info['name'],
            'is_compressor': is_compressor,
            'extraction_depth': current_depth + 1,
        }
        if extraction_path:
            extract_hints['extraction_path'] = extraction_path
        if path_prefix:
            extract_hints['path_prefix'] = path_prefix
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.ARCHIVE_EXTRACT.value,
            hints=extract_hints
        )
        queued_count += 1

    summary = f"Detected {archive_count} archives ({compressor_count} compressors), queued {queued_count} for extraction"
    if depth_limit_exceeded > 0:
        summary += f", {depth_limit_exceeded} at depth limit"

    self.complete_analysis(
        analysis_id,
        summary=summary,
        details=json.dumps({
            'archives_found': archive_count,
            'compressors_found': compressor_count,
            'depth_limit_exceeded': depth_limit_exceeded
        })
    )


@analysis_handler("archive extraction")
def process_archive_extract(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process ARCHIVE_EXTRACT analysis.
    Extracts a specific archive file and registers the extracted files.

    When partition_uuid is present in hints, extracts an archive found
    inside a disc image (the original flow).  When partition_uuid is
    absent, the artefact itself is the archive (top-level upload) and
    the handler creates a new partition for the extracted files.
    """
    import json

    from shared.archive_formats import (
        ArchiveType,
        get_archive_info,
    )

    from ..config import MAX_ARCHIVE_DEPTH, OUTPUT_DIR
    from ..utils.paths import get_output_path

    analysis_id = analysis['id']

    hints = json.loads(analysis.get('hints') or '{}')

    file_id = hints.get('file_id')
    partition_uuid = hints.get('partition_uuid')
    archive_type_str = hints.get('archive_type')
    extraction_depth = hints.get('extraction_depth', 1)
    hinted_extraction_path = hints.get('extraction_path')
    path_prefix = hints.get('path_prefix', '')

    # ── Top-level artefact archive ──────────────────────────────────
    # When no partition_uuid is provided, the artefact itself is the
    # archive (uploaded directly, not found inside a disc image).
    # Derive the archive type from the artefact type and delegate to
    # the shared extraction logic below after creating a partition.
    if not partition_uuid:
        artefact_type = artefact.get('artefact_type', '')
        if not archive_type_str:
            # Map ArtefactType value → ArchiveType value.  Most share
            # the same string values (zip, tar_gz, rar).  ARC needs
            # explicit mapping since ArtefactType.ARC ("arc") covers
            # both ArcFS and Spark; default to ArcFS and let the
            # magic-byte sniff in _extract_top_level_archive correct
            # it to Spark when appropriate.
            _ARTEFACT_TO_ARCHIVE = {
                ArtefactType.ARC.value: ArchiveType.ARCFS.value,
            }
            archive_type_str = _ARTEFACT_TO_ARCHIVE.get(
                artefact_type, artefact_type
            )

    # Get ArchiveType enum from string
    try:
        archive_type = ArchiveType(archive_type_str)
        archive_info = get_archive_info(archive_type)
    except (ValueError, KeyError):
        self.fail_analysis(analysis_id, f'Unknown archive type: {archive_type_str}')
        return

    # ── Top-level artefact archive (continued) ──────────────────────
    # Extract the artefact file directly and create a new partition
    # for the extracted files, then return.
    if not partition_uuid:
        self._extract_top_level_archive(
            analysis, artefact, work_dir,
            archive_type, archive_info,
            extract_zip, extract_tar, extract_rar, extract_7z,
        )
        return

    # Get partition and item metadata from API
    partition_resp = self.api.get(f"/partitions/{partition_uuid}")
    if not partition_resp:
        self.fail_analysis(analysis_id, 'Failed to get partition info')
        return

    partition = partition_resp.get('partition', {})

    # Find the file in the partition
    files_resp = self.api.get(f"/partitions/{partition_uuid}/files?per_page=10000")
    if not files_resp:
        self.fail_analysis(analysis_id, 'Failed to get partition files')
        return

    # Find our specific file
    target_file = None
    for f in files_resp.get('files', []):
        if f['id'] == file_id:
            target_file = f
            break

    if not target_file:
        self.fail_analysis(analysis_id, f'File {file_id} not found in partition')
        return

    # Determine extraction path: prefer value passed through hints (set by the
    # analysis that triggered ARCHIVE_DETECT, which in turn queued this job).
    # Fall back to searching analyses only for jobs created before this fix.
    extraction_path = hinted_extraction_path
    if not extraction_path:
        artefact_uuid = artefact.get('uuid')
        analyses_resp = self.api.get(f"/artefacts/{artefact_uuid}/analysis")
        # Prefer file_extraction (always the disc-level extraction root);
        # only fall through to archive_extract entries if there is no
        # file_extraction with an output_path.
        file_extraction_path = None
        archive_extract_path = None
        for a in analyses_resp.get('analyses', []):
            atype = a.get('analysis_type')
            opath = a.get('output_path')
            if not opath:
                continue
            if atype == 'file_extraction' and not file_extraction_path:
                file_extraction_path = opath
            elif atype == 'archive_extract' and not archive_extract_path:
                archive_extract_path = opath
        extraction_path = file_extraction_path or archive_extract_path

    if not extraction_path:
        self.fail_analysis(analysis_id, 'Could not determine extraction path for files')
        return

    # Construct full path to archive file.
    # For nested archives, the DB path includes parent archive prefixes
    # (e.g. "OuterArchive/InnerArchive.zip") but on disk the file is
    # relative to the extraction directory without those prefixes.
    # Strip the path_prefix to get the on-disk relative path.
    db_path = target_file['path']
    if path_prefix and db_path.startswith(path_prefix + '/'):
        disk_relative_path = db_path[len(path_prefix) + 1:]
    else:
        disk_relative_path = db_path

    risc_os_filetype = target_file.get('risc_os_filetype')

    # Download only the single file needed — not the entire extraction
    # tree.  In S3 mode this avoids downloading thousands of files just
    # to read one archive.
    archive_path = self._resolve_single_extraction_file(
        extraction_path, disk_relative_path, work_dir,
        risc_os_filetype=risc_os_filetype,
    )
    if not archive_path:
        self.fail_analysis(
            analysis_id,
            f'Archive file not found: {disk_relative_path} '
            f'(extraction_path={extraction_path})',
        )
        return

    # Get item for hierarchical path
    item = artefact.get('item', {'uuid': 'default', 'slug': 'default'})

    # Extract archive to temporary directory first
    temp_output_dir = work_dir / 'archive_contents'

    # Sniff magic bytes — filetype-based detection can be wrong (e.g.
    # &DDC is used for both Spark and ZIP on RISC OS).  Override the
    # archive_type when the file header tells us otherwise.
    sniffed = self._sniff_archive_magic(archive_path)
    if sniffed is not None and sniffed != archive_type:
        log.info(f"Magic-byte sniff overrides {archive_type.value} → {sniffed.value}")
        # A ZIP found via RISC OS filetype should be treated as
        # ZIP_RISCOS so Acorn ,xxx suffixes are parsed correctly.
        _RISCOS_TYPES = (
            ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.PACKDIR,
            ArchiveType.TBAFS, ArchiveType.CFS, ArchiveType.SQUASH,
            ArchiveType.ZIP_RISCOS,
        )
        if sniffed == ArchiveType.ZIP and archive_type in _RISCOS_TYPES:
            sniffed = ArchiveType.ZIP_RISCOS
        archive_type = sniffed
        archive_info = get_archive_info(archive_type)

    # A ZIP detected by extension (no RISC OS filetype) whose contents
    # have ,xxx filetype suffixes is really a RISC OS ZIP.
    if archive_type == ArchiveType.ZIP and self._is_riscos_zip(archive_path):
        log.info("ZIP contains Acorn extra-field (0x4341) metadata — upgrading to ZIP_RISCOS")
        archive_type = ArchiveType.ZIP_RISCOS
        archive_info = get_archive_info(archive_type)

    # Choose extraction method based on archive type.
    # FCFS requires a conversion step with a different source path,
    # so handle it before the general dispatch table.
    if archive_type == ArchiveType.FCFS:
        raw_path = work_dir / 'converted.img'
        conv_result = convert_fcfs_to_raw(archive_path, raw_path)
        if not conv_result['success']:
            result = conv_result
        else:
            # Extract the converted image
            result = extract_acorn_disc_image_manager(raw_path, temp_output_dir)
    else:
        from functools import partial

        _dispatch = {
            ArchiveType.SPARK:      extract_riscosarc,
            ArchiveType.ARCFS:      extract_riscosarc,
            ArchiveType.PACKDIR:    extract_riscosarc,
            ArchiveType.CFS:        extract_riscosarc,
            ArchiveType.SQUASH:     extract_riscosarc,
            ArchiveType.TBAFS:      extract_tbafs,
            ArchiveType.XFILES:     extract_xfiles,
            ArchiveType.DOSDISC:    extract_dos_7z,
            ArchiveType.ZIP_RISCOS: extract_zip_riscos,
            ArchiveType.ZIP:        extract_zip,
            ArchiveType.TAR:        partial(extract_tar, archive_type=ArchiveType.TAR.value),
            ArchiveType.TARGZ:      partial(extract_tar, archive_type=ArchiveType.TARGZ.value),
            ArchiveType.TARBZ2:     partial(extract_tar, archive_type=ArchiveType.TARBZ2.value),
            ArchiveType.TARXZ:      partial(extract_tar, archive_type=ArchiveType.TARXZ.value),
            ArchiveType.RAR:        extract_rar,
            ArchiveType.SEVENZ:     extract_7z,
            # Single-file compressors: output file keeps the name minus the compression extension
            ArchiveType.GZIP:       lambda ap, tod: decompress_single_file(ap, tod / ap.stem, ArchiveType.GZIP.value),
            ArchiveType.BZIP2:      lambda ap, tod: decompress_single_file(ap, tod / ap.stem, ArchiveType.BZIP2.value),
            ArchiveType.XZ:         lambda ap, tod: decompress_single_file(ap, tod / ap.stem, ArchiveType.XZ.value),
            ArchiveType.ZSTD:       lambda ap, tod: decompress_single_file(ap, tod / ap.stem, ArchiveType.ZSTD.value),
        }

        extractor = _dispatch.get(archive_type)
        if extractor is None:
            self.fail_analysis(analysis_id, f'Unsupported archive type: {archive_type.value}')
            return

        result = extractor(archive_path, temp_output_dir)

        # SparkFS filetypes Zip files as &DDC (Archive), which is also
        # used for Spark. If riscosarc unpacking fails, try Zip.
        # Upgrade archive_type to ZIP_RISCOS so the display name reflects
        # the actual container format while keeping is_acorn_archive True.
        if not result['success'] and archive_type == ArchiveType.SPARK:
            result = extract_zip_riscos(archive_path, temp_output_dir)
            if result['success']:
                archive_type = ArchiveType.ZIP_RISCOS
                archive_info = get_archive_info(archive_type)

    if not result['success']:
        self.fail_analysis(
            analysis_id,
            result.get('error', 'Extraction failed'),
            tool_name=result.get('tool'),
            details=json.dumps({'process_output': result.get('process_output')})
        )
        return

    # Create persistent output directory only after successful extraction
    persistent_output = get_output_path(
        OUTPUT_DIR,
        item,
        artefact,
        analysis,
        partition
    )

    # Move extracted files from temp to persistent storage
    if temp_output_dir.exists():
        persistent_output.mkdir(parents=True, exist_ok=True)
        shutil.copytree(temp_output_dir, persistent_output, dirs_exist_ok=True)

    # Scan extracted files from persistent storage.
    # Paths are stored relative to the extraction output.  The API
    # will automatically prefix them with the archive's path when
    # registering (based on parent_file_id).
    archive_display_path = target_file['path']

    # RISC OS archive extractors preserve ,xxx filetype suffixes on
    # filenames.  Parse these to populate risc_os_filetype and strip
    # the suffix from display paths (same logic as FILE_EXTRACTION).
    is_acorn_archive = archive_type in (
        ArchiveType.ARCFS, ArchiveType.SPARK, ArchiveType.ZIP_RISCOS,
        ArchiveType.PACKDIR, ArchiveType.TBAFS, ArchiveType.CFS,
        ArchiveType.SQUASH, ArchiveType.FCFS,
    )

    files = enumerate_extracted_files(
        persistent_output,
        acorn=is_acorn_archive,
        parent_file_id=file_id,
        extraction_depth=extraction_depth,
        inf_metadata=result.get('inf_metadata'),
    )

    # Register extracted files in the same partition with parent_file_id
    if files:
        self.api.post_file_records(partition_uuid, files)

    # Upload extraction tree to storage (no-op in local mode) BEFORE
    # queueing follow-up analyses.  Otherwise workers can claim the
    # queued jobs and fail to fetch files from S3 that have not been
    # uploaded yet.
    self._upload_extraction_tree(persistent_output)
    rel_output_path = self._relative_output_path(persistent_output)

    # Queue ARCHIVE_DETECT for nested archives (if under depth limit).
    # Pass the archive's display path as path_prefix so that nested
    # ARCHIVE_EXTRACT jobs can strip it to locate files on disk.
    if extraction_depth < MAX_ARCHIVE_DEPTH:
        self.api.queue_analysis(
            artefact['uuid'],
            AnalysisType.ARCHIVE_DETECT.value,
            hints={
                'partition_uuid': partition_uuid,
                'extraction_path': rel_output_path,
                'path_prefix': archive_display_path,
            }
        )

    # Re-queue PRODUCT_RECOGNITION so the newly-extracted files are
    # included in folder matching.  The first PRODUCT_RECOGNITION run
    # (queued after the outer extraction) fires before this archive's
    # contents are registered, so it cannot see them.
    self.api.queue_analysis(
        artefact['uuid'],
        AnalysisType.PRODUCT_RECOGNITION.value,
        hints={'partition_uuid': partition_uuid},
    )

    # Queue FORMAT_CONVERT to scan for and convert any Sprite/Draw/Text files.
    # Pass path_prefix so that source_file values in analysis.details match
    # ExtractedFile.path in the database (which has the archive's display
    # path prepended for nested archives).
    self.api.queue_analysis(
        artefact['uuid'],
        AnalysisType.FORMAT_CONVERT.value,
        hints={
            'extraction_path': rel_output_path,
            'path_prefix': archive_display_path,
            'partition_uuid': partition_uuid,
        },
    )

    # Queue RISCOS_MODULE_PARSE for modules inside the archive.
    # The initial parse (queued by queue_partition_follow_ups after
    # FILE_EXTRACTION) runs before archive contents are registered,
    # so it never sees files inside archives.
    # Pass path_prefix so the handler can strip the archive prefix when
    # building on-disk paths (DB paths include the prefix, disk paths don't).
    self.api.queue_analysis(
        artefact['uuid'],
        AnalysisType.RISCOS_MODULE_PARSE.value,
        hints={
            'partition_uuid': partition_uuid,
            'extraction_path': rel_output_path,
            'path_prefix': archive_display_path,
        },
    )

    tool_key = result.get('tool', 'tool').lower().replace(' ', '_')
    po = result.get('process_output')
    details: dict = {
        'file_count': len(files),
        'extraction_depth': extraction_depth,
        'archive_type': archive_type.value,
    }
    if po:
        details[tool_key] = {'process_output': po}

    self.complete_analysis(
        analysis_id,
        tool_name=result['tool'],
        output_path=rel_output_path,
        summary=f"Extracted {len(files)} files from {archive_info['name']} archive",
        details=json.dumps(details)
    )
# vim: ts=4 sw=4 et
