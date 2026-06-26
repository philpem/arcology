"""
Analysis-hint key constants.

The worker passes per-job context between analyses as a free-form ``hints``
dict: it is queued via the REST API (``queue_analysis(hints=...)``) and read
back in each handler (``hints.get('partition_uuid')``).  These keys were
historically stringly-typed and duplicated across ~6 worker modules and the
CLI, where a typo silently produced a *missing* hint (``None``) rather than an
error.

This module is the single source of truth for those key names.  Referencing
``HintKey.PARTITION_UUID`` raises ``AttributeError`` at import/run time on a
typo, whereas ``hints['partiton_uuid']`` would not.  It lives in
``arcology_shared`` alongside :class:`~arcology_shared.enums.AnalysisStatus`
so the worker (``worker/arcworker/``) and the CLI (``cli/arccli/``) share one
definition.

The string *values* are the wire format stored in ``Analysis.hints`` JSON and
must not change without a data migration.
"""


class HintKey:
    """String constants for analysis-hint dict keys.

    Grouped by the pipeline stage that sets each hint.  Use these constants
    instead of bare string literals when writing or reading the ``hints``
    dict so that typos surface as ``AttributeError`` rather than silent
    ``None`` lookups.
    """

    # --- Partition / extraction context ---
    # Set by PARTITION_DETECT, consumed by FILE_EXTRACTION / ARMLOCK_REMOVE
    # and the metadata/protection analysers.
    PARTITION_UUID = 'partition_uuid'
    PARTITION_INDEX = 'partition_index'
    PARTITION_INDEX_BASE = 'partition_index_base'
    PARTITION_IMAGE_PATH = 'partition_image_path'
    FILESYSTEM = 'filesystem'
    CONTAINER_FORMAT = 'container_format'

    # --- Archive / nested-extraction context ---
    # Threaded through ARCHIVE_EXTRACT and the follow-on analyses queued after
    # extraction so extracted-file paths resolve correctly.
    EXTRACTION_PATH = 'extraction_path'
    PATH_PREFIX = 'path_prefix'
    EXTRACTION_DEPTH = 'extraction_depth'
    FILE_ID = 'file_id'
    ARCHIVE_TYPE = 'archive_type'

    # --- Flux decode hints ---
    DFI_CLOCK_MHZ = 'dfi_clock_mhz'
    GW_FORMAT = 'gw_format'

    # --- User-supplied upload hints (CLI ``arco upload --hint``) ---
    # The web app forwards these to every auto-queued analysis.  FILESYSTEM
    # and DFI_CLOCK_MHZ (above) are also user-settable.
    PLATFORM = 'platform'

    # --- CLEANUP job payload keys ---
    ARTEFACT_KEYS = 'artefact_keys'
    OUTPUT_FILE_KEYS = 'output_file_keys'
    OUTPUT_DIR_PREFIXES = 'output_dir_prefixes'
    CACHE_PREFIXES = 'cache_prefixes'


# Upload hints a user may pass via ``arco upload --hint KEY=VALUE``.  Single
# source of truth for the CLI help text, the client docstring, and the
# integer-coercion set below — keep new user-facing hints listed here.
UPLOAD_HINT_KEYS: tuple[str, ...] = (
    HintKey.DFI_CLOCK_MHZ,
    HintKey.PLATFORM,
    HintKey.FILESYSTEM,
)

# Subset of UPLOAD_HINT_KEYS whose values must be coerced to ``int`` before
# being sent to the server.
UPLOAD_HINT_INT_KEYS: frozenset[str] = frozenset({HintKey.DFI_CLOCK_MHZ})

# vim: ts=4 sw=4 et
