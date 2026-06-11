"""
ARMlock disc-protection removal handler.

ARMlock (by Digital Services) protects ADFS discs by replacing the real
root directory with a stripped "demo" copy and stashing the original at
disc address 0x400.  PARTITION_DETECT queues this handler whenever the
ARMlock signature is found on an ADFS partition.
"""

import json
from pathlib import Path
from shared.enums import AnalysisType, ArtefactType
from ..config import log
from ..tools import detect_armlock, remove_armlock
from ._common import analysis_handler
from ._protections import PROTECTION_SCHEMES, ProtectionScheme

SCHEME = ProtectionScheme(
    name='armlock',
    applicable_filesystems=frozenset({'adfs'}),
    analysis_type=AnalysisType.ARMLOCK_REMOVE,
    detect=lambda path: detect_armlock(path).get('detected', False),
)
PROTECTION_SCHEMES.append(SCHEME)


@analysis_handler("ARMlock removal", AnalysisType.ARMLOCK_REMOVE)
def process_armlock_remove(self, analysis: dict, artefact: dict, work_dir: Path):
    """Remove ARMlock disc security from a confirmed-protected ADFS disc image.

    This handler is only queued by PARTITION_DETECT when the ARMlock signature
    has already been found on an ADFS partition.  It re-runs detection to capture
    full details (zone map state, directory listings, ARMlock module bytes), then
    removes the protection and hands off to FILE_EXTRACTION.

    ARMlock (by Digital Services) protects ADFS discs by replacing the real root
    directory with a stripped "demo" copy and stashing the original at disc address
    0x400.  It also encodes the boot_option field in both copies of the zone map.
    Disc Image Manager cannot extract files from a protected image because the root
    directory it sees is fake.
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')
    filesystem_hint = hints.get('filesystem', 'adfs')
    partition_index = hints.get('partition_index', 0)

    # Use cached decompressed path if available (from PARTITION_DETECT).
    input_path = self._resolve_partition_image(
        hints.get('partition_image_path'), artefact, work_dir)

    # Re-run detection to capture full details for the analysis record.
    detection = detect_armlock(input_path)

    if not detection.get('detected'):
        # Shouldn't happen (PARTITION_DETECT confirmed protection), but handle
        # it defensively rather than leaving FILE_EXTRACTION unqueued.
        log.warning(
            f"ARMLOCK_REMOVE for analysis {analysis_id}: protection not found on "
            f"second pass — queuing FILE_EXTRACTION directly"
        )
        self.queue_file_extraction(
            artefact['uuid'],
            filesystem_hint,
            partition_index,
            partition_image_path=hints.get('partition_image_path'),
        )
        self.complete_analysis(
            analysis_id,
            tool_name='armlock',
            summary='ARMlock signature not found on second pass; FILE_EXTRACTION queued directly',
            details=json.dumps(detection),
        )
        return

    # Remove protection and register a cleaned artefact.
    cleaned_path = work_dir / 'armlock_removed.img'
    removal = remove_armlock(input_path, cleaned_path)

    # Serialise module bytes as hex for JSON storage; also register as a
    # derived UNKNOWN artefact so it can be downloaded for offline analysis.
    # The module contains the protection code and password data.
    # Exclude raw module bytes (stored separately as an artefact) and the
    # real_root listing (FILE_EXTRACTION on the cleaned image covers this).
    details: dict = {k: v for k, v in detection.items()
                     if k not in ('module_data', 'real_root', 'stripped_root')}
    if detection.get('module_data'):
        details['module_data_length'] = len(detection['module_data'])
        module_path = work_dir / 'ARMlock_module'
        module_path.write_bytes(detection['module_data'])
        module_label = f'{artefact.get("label", "Unknown")} (ARMlock module)'
        if detection.get('module_version'):
            module_label = f'{artefact.get("label", "Unknown")} (ARMlock module {detection["module_version"]})'
        module_artefact = self.api.register_derived_artefact(
            analysis_id,
            module_label,
            module_path,
            ArtefactType.UNKNOWN,
            auto_analyse=False,
        )
        if module_artefact:
            details['module_artefact_uuid'] = module_artefact['artefact']['uuid']
    details['removal'] = removal

    if removal['success']:
        cleaned = self.api.register_derived_artefact(
            analysis_id,
            f'{artefact.get("label", "Unknown")} (ARMlock removed)',
            cleaned_path,
            ArtefactType.RAW_SECTOR,
            auto_analyse=False,
        )
        if cleaned:
            cleaned_uuid = cleaned['artefact']['uuid']
            self.queue_file_extraction(cleaned_uuid, filesystem_hint, partition_index)
            summary = (
                'ARMlock disc security detected and removed. '
                'Cleaned artefact queued for file extraction.'
            )
            if detection.get('module_data'):
                summary += ' ARMlock module saved.'
        else:
            summary = 'ARMlock detected; failed to register cleaned artefact.'
    else:
        summary = f'ARMlock detected but removal failed: {removal.get("error")}'

    self.complete_analysis(
        analysis_id,
        tool_name='armlock',
        summary=summary,
        details=json.dumps(details),
    )
# vim: ts=4 sw=4 et
