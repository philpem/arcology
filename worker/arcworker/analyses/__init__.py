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

from ._common import HANDLERS  # noqa: F401  (re-export)

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
    process_replay,
    process_replay_transcode,
    process_riscos_module_parse,
)
from .partition import process_partition_detect

__all__ = [
    'HANDLERS',
    'process_cleanup',
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
    '_handle_disk_image_bundle',
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
    'process_replay',
    'process_replay_transcode',
    'process_riscos_module_parse',
    # Armlock
    'process_armlock_remove',
    # Partition
    'process_partition_detect',
]
# vim: ts=4 sw=4 et
