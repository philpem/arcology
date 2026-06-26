"""Worker-importable copy of the server's ANALYSIS_MAP.

The real ``ANALYSIS_MAP`` lives in ``myapp/services/artefact_types.py`` and
imports Flask + SQLAlchemy, so it cannot be imported inside the worker
container (which has no web-app dependencies).  The fake server in
``fake_api.py`` needs the same auto-analysis scheduling to simulate what the
real web app queues when a derived artefact is registered, so we keep a small
duplicate here that imports *only* ``arcology_shared.enums``.

Drift between this copy and the real map is caught by
``ci/test_integration_analysis_map.py``, which runs in the app-tests job where
Flask is available and asserts the two agree for every type covered here.

Divergence from the server (intentional): the real
``queue_analyses_for_artefact`` unconditionally prepends
``CHECKSUM_COMPUTE``.  The fake records artefact hashes at registration time
instead (the real client computes them too), so this map does NOT include
``CHECKSUM_COMPUTE`` and the fake does not enqueue it.
"""

from arcology_shared.enums import AnalysisType, ArtefactType

# Only the types the integration suite actually exercises need to appear here.
# The guard test asserts each of these matches the real map exactly; types not
# listed are simply not checked (and not simulated).
IT_ANALYSIS_MAP = {
    ArtefactType.ZIP: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.TARGZ: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.SEVENZ: [AnalysisType.ARCHIVE_EXTRACT],

    # Promotion targets: a disc image extracted from an archive is registered
    # as a derived artefact of one of these types, which queues PARTITION_DETECT.
    ArtefactType.RAW_SECTOR: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_ZST: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_GZ: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_BZ2: [AnalysisType.PARTITION_DETECT],
}

# vim: ts=4 sw=4 et
