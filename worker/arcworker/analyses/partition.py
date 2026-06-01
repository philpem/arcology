"""
Partition / filesystem detection handler.

Detects partitions and filesystem types in raw disc images, registers
each partition as a derived artefact (or carves the whole image as a
single partition), and queues FILE_EXTRACTION (or ARMLOCK_REMOVE for
ARMlock-protected ADFS discs) on the resulting partitions.
"""

import json
import shutil
from pathlib import Path
from shared.enums import ArtefactType
from ..compression import extract_partition_range, is_region_uniform
from ..config import log
from ..tools import (
    detect_acorn_adfs,
    detect_acorn_partitions,
    detect_fat_filesystem,
    detect_format_file_cmd,
    detect_partitions_sfdisk,
)
from ._common import analysis_handler
from ._protections import queue_extraction_or_protection_remove


@analysis_handler("partition detection")
def process_partition_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process PARTITION_DETECT analysis.
    Detects partitions and filesystem types in raw disc images.

    Detection order:
    1. Acorn partition schemes (Nexus, HCCS, Simtec, etc.) — checked
       first because they have specific magic bytes and don't depend on
       ADFS boot block checksums.  Nexus is tried before HCCS because
       on Nexus discs the area at 0xC00 holds sharer firmware (not an
       HCCS boot block).  A PC disc reformatted as Acorn may retain a
       stale MBR, so sfdisk must come after.
    2. ADFS filesystem signatures — for unpartitioned Acorn discs.
    3. sfdisk — standard MBR/GPT partition tables.

    Fully-decoded schemes (Nexus, HCCS) provide per-partition metadata
    including disc names and access information.  Signature-only
    schemes (Simtec) are noted but fall through to whole-disc handling.

    All partition schemes normalise to byte-based offsets (start_byte /
    size_bytes) so the carving and gap-detection code below is
    addressing-mode agnostic.

    When multiple partitions are detected (or a single partition that
    doesn't span the whole disc), each partition is extracted and
    registered as a derived artefact with FILE_EXTRACTION queued
    against it.  Any unpartitioned gaps between/around partitions are
    also preserved as derived artefacts unless the gap is uniform
    (every byte the same value), in which case it is omitted with a
    note in the summary.

    For single-partition whole-disc images (ADFS, no partition table),
    FILE_EXTRACTION is queued against the original artefact.  When the
    original file was compressed, a decompressed copy is cached so
    that FILE_EXTRACTION doesn't have to decompress again.
    """
    analysis_id = analysis['id']

    input_path = self.get_input_path(artefact, work_dir)
    decompression_info = self._decompression_info  # Capture before it's overwritten
    hints = json.loads(analysis.get('hints') or '{}')
    filesystem_hint = hints.get('filesystem', '').lower()

    results = {}
    detected_partitions = []

    # 1. Try Acorn partition schemes (HCCS, Simtec, etc.) first.
    # These check for specific magic bytes at known boot block offsets
    # and must run before sfdisk, which would report stale MBR partition
    # tables left behind when a PC disc is reformatted as Acorn.
    # This does NOT depend on ADFS signature detection — the boot block
    # checksum can fail even on a valid HCCS disc (e.g. if the checksum
    # byte is missing or the first sector contains a stale MBR).
    acorn_result = detect_acorn_partitions(input_path)
    results['acorn_partitions'] = acorn_result

    if acorn_result['detected'] and acorn_result.get('partitions'):
        # Fully decoded scheme (e.g. HCCS) with partition list
        detected_partitions = acorn_result['partitions']
    elif acorn_result['detected']:
        # Signature-only scheme (e.g. Simtec) — no decoded partitions.
        # Treat as single ADFS partition spanning the whole disc.
        detected_partitions = [{
            'index': 0,
            'start_byte': 0,
            'filesystem': 'adfs',
            'description': acorn_result.get('description', ''),
            'size_bytes': input_path.stat().st_size,
        }]

    # 2. Check for ADFS filesystem signatures.
    # Always run for metadata (signatures are useful in the summary),
    # but only use for partitioning if no Acorn scheme was found above.
    adfs_result = detect_acorn_adfs(input_path)
    results['adfs'] = adfs_result

    if not detected_partitions and adfs_result.get('adfs_detected'):
        _adfs_subformat = adfs_result.get('adfs_subformat')
        _adfs_label = (
            f'Acorn ADFS {_adfs_subformat}' if _adfs_subformat else 'Acorn ADFS'
        )
        detected_partitions = [{
            'index': 0,
            'start_byte': 0,
            'filesystem': 'adfs',
            'description': _adfs_label,
            'container_format': _adfs_label,
            'size_bytes': input_path.stat().st_size,
            'signatures': adfs_result.get('signatures', []),
        }]

    # 3. If no Acorn formats, try sfdisk for standard partition tables (MBR/GPT)
    if not detected_partitions:
        sfdisk_result = detect_partitions_sfdisk(input_path)
        results['sfdisk'] = sfdisk_result

        if sfdisk_result['success']:
            detected_partitions = sfdisk_result['partitions']

    # 4. Use file command for additional format info
    file_result = detect_format_file_cmd(input_path)
    results['file'] = file_result

    # 5. If nothing detected, report whole disc as single unknown partition.
    # Use boot-sector BPB parsing to identify FAT12/16/32 before falling
    # back to 'unknown'.  The 'file' command output is intentionally not
    # used for this decision because its output format is not stable enough
    # for machine parsing.
    if not detected_partitions:
        file_size = input_path.stat().st_size
        inferred_fs = detect_fat_filesystem(input_path) or 'unknown'
        detected_partitions = [{
            'index': 0,
            'start_byte': 0,
            'filesystem': filesystem_hint or inferred_fs,
            'description': 'No partition table detected (whole disc)',
            'size_bytes': file_size,
        }]

    # Offset partition indices by partition_index_base when set.  This is used
    # by the independent-sides split (FLUX_DECODE): each physical side becomes
    # its own single-sided artefact, but because they share the parent disc's
    # partition list in the UI they would all show as "partition 0" and be
    # indistinguishable.  Numbering side 1's partition(s) from base 1 makes the
    # aggregated list read "partition 0" / "partition 1" (i.e. side 0 / side 1).
    partition_index_base = hints.get('partition_index_base', 0)
    if partition_index_base:
        for _p in detected_partitions:
            _p['index'] += partition_index_base

    # -----------------------------------------------------------------
    # Decide how to persist partition images for FILE_EXTRACTION
    # -----------------------------------------------------------------
    disc_size = input_path.stat().st_size
    is_compressed = decompression_info is not None

    # Register partitions as derived artefacts when there are multiple
    # partitions, or a single partition that doesn't span the whole disc.
    # This also lets us extract and preserve unpartitioned space (gaps).
    # All partition schemes normalise to start_byte / size_bytes so the
    # logic below is addressing-mode agnostic.
    should_register_derived = False
    if len(detected_partitions) > 1:
        should_register_derived = True
    elif len(detected_partitions) == 1:
        p = detected_partitions[0]
        p_start = p.get('start_byte', 0)
        p_end = p_start + p.get('size_bytes', disc_size)
        if p_start > 0 or p_end < disc_size:
            should_register_derived = True

    unpartitioned_notes: list[str] = []
    partition_image_paths: dict[int, str] = {}
    artefact_label = artefact.get('label', 'Unknown')

    if should_register_derived:
        # ---- Extract each partition as a derived artefact ----
        for partition in detected_partitions:
            idx = partition['index']
            fs = partition.get('filesystem', 'unknown')
            start_byte = partition.get('start_byte', 0)
            size_bytes = partition.get('size_bytes', 0)

            # Build a descriptive label including disc name when available
            disc_name = partition.get('disc_name')
            if disc_name:
                part_label = f"{artefact_label} (partition {idx}: {disc_name}, {fs})"
            else:
                part_label = f"{artefact_label} (partition {idx}, {fs})"

            partition_path = work_dir / f"partition_{idx}.img"
            extract_partition_range(input_path, partition_path, start_byte, size_bytes)

            derived = self.api.register_derived_artefact(
                analysis_id,
                part_label,
                partition_path,
                ArtefactType.RAW_SECTOR,
                auto_analyse=False
            )
            if derived:
                derived_uuid = derived['artefact']['uuid']
                part_cf = partition.get('container_format')
                # Nexus printer partitions are not Filecore formatted; skip
                # FILE_EXTRACTION so they are only registered as downloadable
                # raw artefacts (see issue #89).
                is_nexus_printer = partition.get('nexus_flags', {}).get('printer', False)
                if not is_nexus_printer:
                    queue_extraction_or_protection_remove(
                        self, derived_uuid, partition_path, fs, idx,
                        container_format=part_cf,
                    )
            else:
                log.error(f"Failed to register partition {idx} as derived artefact")

        # ---- Handle unpartitioned space (gaps) ----
        sorted_parts = sorted(
            detected_partitions,
            key=lambda p: p.get('start_byte', 0)
        )

        gaps: list[dict] = []
        # Space before first partition.
        # On Nexus discs this region contains the disc sharer firmware.
        first_start = sorted_parts[0].get('start_byte', 0)
        if first_start > 0:
            if acorn_result.get('scheme') == 'nexus':
                pre_label = 'Nexus firmware'
            else:
                pre_label = 'Pre-partition space'
            gaps.append({'start': 0, 'size': first_start, 'label': pre_label})
        # Space between consecutive partitions
        for i in range(len(sorted_parts) - 1):
            end_curr = sorted_parts[i].get('start_byte', 0) + sorted_parts[i].get('size_bytes', 0)
            start_next = sorted_parts[i + 1].get('start_byte', 0)
            if start_next > end_curr:
                gaps.append({
                    'start': end_curr, 'size': start_next - end_curr,
                    'label': f'Gap between partitions {sorted_parts[i]["index"]} and {sorted_parts[i + 1]["index"]}',
                })
        # Space after last partition
        last_end = sorted_parts[-1].get('start_byte', 0) + sorted_parts[-1].get('size_bytes', 0)
        if last_end < disc_size:
            gaps.append({
                'start': last_end, 'size': disc_size - last_end,
                'label': 'Post-partition space',
            })

        for gap in gaps:
            uniform, fill_byte = is_region_uniform(input_path, gap['start'], gap['size'])
            if uniform:
                note = (
                    f'{gap["label"]} ({gap["size"]:,} bytes) omitted: '
                    f'uniform fill 0x{fill_byte:02X}'
                )
                unpartitioned_notes.append(note)
                log.info(f"Skipping {note}")
            else:
                gap_path = work_dir / f"unpartitioned_{gap['start']:#x}.img"
                extract_partition_range(input_path, gap_path, gap['start'], gap['size'])
                self.api.register_derived_artefact(
                    analysis_id,
                    f"{artefact_label} ({gap['label']})",
                    gap_path,
                    ArtefactType.UNKNOWN,
                    auto_analyse=False
                )
    else:
        # Single partition covering the whole disc (ADFS, no partition
        # table, or sfdisk single-partition spanning entire image).
        # For compressed files, cache the decompressed image so
        # FILE_EXTRACTION doesn't have to decompress again.
        if is_compressed:
            cache_dir = self.outputs / '.cache' / artefact['uuid']
            cache_dir.mkdir(parents=True, exist_ok=True)
            for partition in detected_partitions:
                idx = partition['index']
                partition_path = cache_dir / f"partition_{idx}.img"
                shutil.copy(input_path, partition_path)
                # Upload cached partition to storage (no-op in local mode)
                cache_key = self.storage.storage_key(
                    'outputs', f".cache/{artefact['uuid']}/partition_{idx}.img")
                self.storage.put(cache_key, partition_path)
                partition_image_paths[idx] = str(partition_path)
                log.info(f"Cached decompressed image as {partition_path}")

        # Queue FILE_EXTRACTION (or ARMLOCK_REMOVE if protected) against
        # the original artefact.
        for partition in detected_partitions:
            idx = partition['index']
            fs = partition.get('filesystem', 'unknown')
            part_cf = partition.get('container_format')
            cached = partition_image_paths.get(idx)
            image_path = Path(cached) if cached else input_path
            queue_extraction_or_protection_remove(
                self, artefact['uuid'], image_path, fs, idx,
                container_format=part_cf,
                partition_image_path=cached,
            )

    # -----------------------------------------------------------------
    # Build summary and details
    # -----------------------------------------------------------------
    fs_types = [p.get('filesystem', 'unknown') for p in detected_partitions]
    summary = ''

    if decompression_info:
        summary += (
            f'Decompressed {decompression_info["compressed_name"]} → '
            f'{decompression_info["decompressed_name"]} '
            f'({decompression_info["decompressed_size"]:,} bytes). '
        )

    summary += f'Detected {len(detected_partitions)} partition(s): {", ".join(fs_types)}'

    # Acorn partition scheme info
    acorn_result = results.get('acorn_partitions', {})
    if acorn_result.get('detected'):
        scheme = acorn_result.get('scheme', 'unknown')
        if acorn_result.get('partitions'):
            summary += f' [{scheme.upper()} partitioning]'
            # Include disc names in summary
            names = [p.get('disc_name', '') for p in acorn_result['partitions'] if p.get('disc_name')]
            if names:
                summary += f' (volumes: {", ".join(names)})'
        elif acorn_result.get('description'):
            summary += f'. {acorn_result["description"]}'

    if adfs_result.get('adfs_detected') and not acorn_result.get('detected'):
        summary += f' (ADFS signatures: {", ".join(adfs_result.get("signatures", []))})'

    if file_result.get('file_type'):
        summary += f' [file: {file_result["file_type"][:200]}]'

    if unpartitioned_notes:
        summary += '. ' + '; '.join(unpartitioned_notes)

    # Store each tool's result at the top level so the template's Process Logs
    # section can find process_output for sfdisk and file (adfs detection is
    # pure-Python and produces no subprocess output).
    details: dict = {'partitions': detected_partitions}
    details.update(results)
    if decompression_info:
        details['decompression'] = decompression_info
    if partition_image_paths:
        details['cached_partitions'] = partition_image_paths
    if unpartitioned_notes:
        details['unpartitioned_notes'] = unpartitioned_notes

    self.complete_analysis(
        analysis_id,
        tool_name='sfdisk,adfs_detect,file',
        summary=summary,
        details=json.dumps(details)
    )
# vim: ts=4 sw=4 et
