"""
Analysis-handler subpackage.

Each handler is a free function with the signature
``(self, analysis, artefact, work_dir)`` decorated with
``@analysis_handler(description, AnalysisType.X)``.  The decorator (in
``_common``) wraps the function with standard error reporting and
registers it in :data:`HANDLERS` keyed by ``AnalysisType.value`` — that
decorator argument is the single wiring point for dispatch.

``AnalysisWorker.process_analysis()`` looks the handler up in
``HANDLERS`` and calls it with itself as ``self``.  Adding a new
analysis type therefore needs only the decorated function in one of the
modules imported below (plus the usual enum/migration steps in
CLAUDE.md).

A few private helpers and data tables are additionally bound onto
``AnalysisWorker`` as class attributes (see analysis.py) because handler
bodies reference them via ``self.``.
"""

from ._common import HANDLERS

# Importing the handler modules is what populates HANDLERS — keep every
# handler module listed here.
from .armlock import process_armlock_remove
from .cleanup import process_cleanup
from .extraction import (
    _PROMOTABLE_EXTENSIONS,
    _apply_pling_renames,
    _extract_top_level_archive,
    _handle_disk_image_bundle,
    _is_riscos_zip,
    _sniff_archive_magic,
    detect_and_queue_archives,
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
from .images import (
    _EXT_VIEWABLE,
    _RISCOS_VIEWABLE_SUFFIXES,
    _convert_file_to_outputs,
    _detect_viewable_type,
    process_format_convert,
)
from .media import process_media_transcode
from .metadata import (
    process_checksum_compute,
    process_format_identify,
    process_metadata_extract,
    process_replay,
    process_riscos_module_parse,
)
from .partition import process_partition_detect

__all__ = [
    'HANDLERS',
    '_EXT_VIEWABLE',
    '_PROMOTABLE_EXTENSIONS',
    '_RISCOS_VIEWABLE_SUFFIXES',
    '_SCP_VIA_CONVERSION_TYPES',
    '_apply_pling_renames',
    '_convert_file_to_outputs',
    '_detect_viewable_type',
    '_extract_top_level_archive',
    '_handle_disk_image_bundle',
    '_is_riscos_zip',
    '_sniff_archive_magic',
    'detect_and_queue_archives',
    'process_archive_extract',
    # Armlock
    'process_armlock_remove',
    # Metadata
    'process_checksum_compute',
    'process_cleanup',
    'process_detect_track_density',
    'process_disc_mastering_detect',
    'process_disc_protection_detect',
    # Extraction
    'process_file_extraction',
    'process_flux_decode',
    # Flux
    'process_flux_visualisation',
    # Images
    'process_format_convert',
    'process_format_identify',
    # Media
    'process_media_transcode',
    'process_metadata_extract',
    # Partition
    'process_partition_detect',
    'process_replay',
    'process_riscos_module_parse',
]
# vim: ts=4 sw=4 et
