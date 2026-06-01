"""
Analysis-handler subpackage.

Each handler is a free function with the signature
``(self, analysis, artefact, work_dir)`` — they are bound onto
:class:`worker.arcworker.analysis.AnalysisWorker` as methods so the
function bodies (which already reference ``self``) work unchanged.

``HANDLERS`` maps :class:`shared.enums.AnalysisType` values to the
handler attribute name on ``AnalysisWorker``; the dispatch loop in
``process_analysis`` looks up the bound method via ``getattr``.
"""

from shared.enums import AnalysisType
from .armlock import process_armlock_remove
from .extraction import (
    _PROMOTABLE_EXTENSIONS,
    _apply_pling_renames,
    _extract_top_level_archive,
    _is_riscos_zip,
    _sniff_archive_magic,
    process_archive_detect,
    process_archive_extract,
    process_file_extraction,
)
from .flux import (
    _SCP_VIA_CONVERSION_TYPES,
    process_detect_track_density,
    process_disc_mastering_detect,
    process_disc_protection_detect,
    process_flux_decode,
    process_flux_visualisation,
)
from .hashdb import process_hash_rescan
from .images import (
    _EXT_VIEWABLE,
    _RISCOS_HEX_VIEWABLE,
    _RISCOS_VIEWABLE_SUFFIXES,
    _convert_file_to_outputs,
    _detect_viewable_type,
    process_format_convert,
)
from .metadata import (
    process_checksum_compute,
    process_format_identify,
    process_metadata_extract,
    process_product_recognition,
    process_riscos_module_parse,
)
from .partition import process_partition_detect

# AnalysisType.value → method name on AnalysisWorker.  Looked up via
# getattr() in process_analysis().  Kept here (rather than as a dict of
# function refs) so that the dispatch survives any future swap of the
# bound methods on the class.
HANDLERS: dict[str, str] = {
    AnalysisType.CHECKSUM_COMPUTE.value:        'process_checksum_compute',
    AnalysisType.DETECT_TRACK_DENSITY.value:    'process_detect_track_density',
    AnalysisType.FLUX_VISUALISATION.value:      'process_flux_visualisation',
    AnalysisType.FLUX_DECODE.value:             'process_flux_decode',
    AnalysisType.FILE_EXTRACTION.value:         'process_file_extraction',
    AnalysisType.METADATA_EXTRACT.value:        'process_metadata_extract',
    AnalysisType.FORMAT_IDENTIFY.value:         'process_format_identify',
    AnalysisType.PARTITION_DETECT.value:        'process_partition_detect',
    AnalysisType.ARCHIVE_DETECT.value:          'process_archive_detect',
    AnalysisType.ARCHIVE_EXTRACT.value:         'process_archive_extract',
    AnalysisType.PRODUCT_RECOGNITION.value:     'process_product_recognition',
    AnalysisType.DISC_MASTERING_DETECT.value:   'process_disc_mastering_detect',
    AnalysisType.DISC_PROTECTION_DETECT.value:  'process_disc_protection_detect',
    AnalysisType.ARMLOCK_REMOVE.value:          'process_armlock_remove',
    AnalysisType.FORMAT_CONVERT.value:          'process_format_convert',
    AnalysisType.RISCOS_MODULE_PARSE.value:     'process_riscos_module_parse',
    AnalysisType.HASH_RESCAN.value:             'process_hash_rescan',
}


__all__ = [
    'HANDLERS',
    # Flux
    'process_flux_visualisation',
    'process_detect_track_density',
    'process_flux_decode',
    'process_disc_mastering_detect',
    'process_disc_protection_detect',
    '_SCP_VIA_CONVERSION_TYPES',
    # Extraction
    'process_file_extraction',
    'process_archive_detect',
    'process_archive_extract',
    '_apply_pling_renames',
    '_sniff_archive_magic',
    '_is_riscos_zip',
    '_extract_top_level_archive',
    '_PROMOTABLE_EXTENSIONS',
    # Images
    'process_format_convert',
    '_convert_file_to_outputs',
    '_detect_viewable_type',
    '_RISCOS_VIEWABLE_SUFFIXES',
    '_EXT_VIEWABLE',
    '_RISCOS_HEX_VIEWABLE',
    # Metadata
    'process_checksum_compute',
    'process_metadata_extract',
    'process_format_identify',
    'process_product_recognition',
    'process_riscos_module_parse',
    # Armlock
    'process_armlock_remove',
    # Partition
    'process_partition_detect',
    # Hash rescan
    'process_hash_rescan',
]
# vim: ts=4 sw=4 et
