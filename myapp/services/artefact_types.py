"""
Arcology - Artefact type registry

Centralised definitions consumed by the web blueprints, REST API, worker,
and CLI commands:

  EXTENSION_MAP   — filename extension → ArtefactType
  ANALYSIS_MAP    — ArtefactType → list of AnalysisType auto-analyses
  detect_artefact_type()      — extension-based type detection
  queue_analyses_for_artefact() — schedule Analysis rows for an artefact

Nothing here may import from a Flask blueprint or touch request context.
current_app and db.session are fine (they are available in any app context,
including CLI commands and background jobs).
"""

import json
import os
from arcology_shared.enums import AnalysisType, ArtefactType
from ..database import ANALYSIS_PRIORITY_NORMAL, Analysis, AnalysisStatus, Artefact
from ..extensions import db

# Extension to ArtefactType mapping
EXTENSION_MAP = {
    # Flux-level
    '.scp': ArtefactType.SCP,
    '.dfi': ArtefactType.DFI,
    '.a2r': ArtefactType.A2R,

    # Cooked sector-level floppy or hard disc
    '.imd': ArtefactType.IMD,   # needs conversion to sectors
    '.hfe': ArtefactType.HFE,   # needs conversion to sectors

    # Raw sector images
    '.adf': ArtefactType.RAW_SECTOR,
    '.img': ArtefactType.RAW_SECTOR,
    '.ima': ArtefactType.RAW_SECTOR,
    '.dsk': ArtefactType.RAW_SECTOR,

    # CD/DVD
    '.iso': ArtefactType.ISO,

    # Hard drive raw images
    '.dd': ArtefactType.RAW_SECTOR,
    '.hdf': ArtefactType.RAW_SECTOR,

    # Documents
    '.pdf': ArtefactType.PDF,

    # Archives
    '.zip': ArtefactType.ZIP,
    '.tar.gz': ArtefactType.TARGZ,
    '.tgz': ArtefactType.TARGZ,
    '.rar': ArtefactType.RAR,
    '.arc': ArtefactType.ARC,
    '.arcfs': ArtefactType.ARC,
    '.spk': ArtefactType.ARC,
    '.spark': ArtefactType.ARC,
    '.b21':   ArtefactType.TBAFS,
    '.tbafs': ArtefactType.TBAFS,
    '.b23':   ArtefactType.XFILES,

    # Acorn/RISC OS native viewable formats
    '.spr':  ArtefactType.ACORN_SPRITE,
    '.aff':  ArtefactType.ACORN_DRAW,
    '.draw': ArtefactType.ACORN_DRAW,
    '.txt':  ArtefactType.ACORN_TEXT,

    # Common raster images (browser-native pass-through or Pillow-converted)
    '.jpg':  ArtefactType.IMAGE,
    '.jpeg': ArtefactType.IMAGE,
    '.png':  ArtefactType.IMAGE,
    '.gif':  ArtefactType.IMAGE,
    '.webp': ArtefactType.IMAGE,
    '.bmp':  ArtefactType.IMAGE,
    '.tif':  ArtefactType.IMAGE,
    '.tiff': ArtefactType.IMAGE,
    '.pcx':  ArtefactType.IMAGE,
    '.tga':  ArtefactType.IMAGE,

    # Windows vector metafiles (converted to SVG)
    '.wmf':  ArtefactType.IMAGE,
    '.emf':  ArtefactType.IMAGE,
}


def detect_artefact_type(filename: str) -> ArtefactType:
    """Detect artefact type from filename extension."""
    filename_lower = filename.lower()

    # Check compound extensions first (order matters)
    if filename_lower.endswith('.dd.zst'):
        return ArtefactType.DD_ZST
    if filename_lower.endswith('.dd.gz'):
        return ArtefactType.DD_GZ
    if filename_lower.endswith('.dd.bz2'):
        return ArtefactType.DD_BZ2
    if filename_lower.endswith('.tar.gz'):
        return ArtefactType.TARGZ

    # Strip a trailing compression suffix and re-check, so e.g. .dfi.bz2 → .dfi
    stem = filename_lower
    for suffix in ('.gz', '.bz2', '.zst'):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    _, ext = os.path.splitext(stem)
    return EXTENSION_MAP.get(ext, ArtefactType.UNKNOWN)


# Analysis types queued automatically when an artefact of each type is uploaded.
# CHECKSUM_COMPUTE is always prepended unconditionally; it need not appear here.
ANALYSIS_MAP = {
    # Flux images - visualisation and decode attempt.
    # SCP: only DETECT_TRACK_DENSITY and METADATA_EXTRACT are queued at upload time.
    # DETECT_TRACK_DENSITY queues FLUX_VISUALISATION and FLUX_DECODE on the correct
    # target (original SCP if no mismatch; density-corrected SCP if 40-in-80 detected),
    # preventing duplicate HFE/IMD/RAW_SECTOR artefacts from both images.
    ArtefactType.SCP: [AnalysisType.DETECT_TRACK_DENSITY, AnalysisType.METADATA_EXTRACT],
    ArtefactType.DFI: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],
    ArtefactType.A2R: [AnalysisType.FLUX_VISUALISATION, AnalysisType.FLUX_DECODE, AnalysisType.METADATA_EXTRACT],

    # Sector-level floppy - file extraction only works on raw sector images
    # IMD is track-based format with metadata, HFE is an emulator container format
    # These need conversion to IMG (raw sectors) before file extraction can work.
    # FLUX_DECODE is included so that standalone HFE/IMD uploads trigger extraction
    # (same pipeline as SCP, starting from wherever in the chain the source sits).
    ArtefactType.IMD: [AnalysisType.METADATA_EXTRACT, AnalysisType.FLUX_DECODE],
    ArtefactType.HFE: [AnalysisType.FLUX_VISUALISATION, AnalysisType.DISC_MASTERING_DETECT, AnalysisType.DISC_PROTECTION_DETECT, AnalysisType.FLUX_DECODE],

    # CD/DVD - file extraction
    ArtefactType.ISO: [AnalysisType.METADATA_EXTRACT, AnalysisType.FILE_EXTRACTION],

    # Raw sector images - run PARTITION_DETECT first; it queues FILE_EXTRACTION
    # with the detected filesystem hint so the right tool (DIM vs 7z) is used.
    # FILE_EXTRACTION must NOT be queued here directly, as it would race with
    # PARTITION_DETECT and fall back to the wrong tool (7z for ADFS discs, etc.).
    ArtefactType.RAW_SECTOR: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_ZST: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_GZ: [AnalysisType.PARTITION_DETECT],
    ArtefactType.DD_BZ2: [AnalysisType.PARTITION_DETECT],

    # Documents/images - just metadata/checksums
    ArtefactType.PDF: [AnalysisType.METADATA_EXTRACT],

    # Sidecar/companion files (a disk image's ddrescue .map, readme, checksums)
    # have NO automatic analyses — they exist to be viewed/downloaded alongside
    # the image.  (CHECKSUM_COMPUTE is still queued on direct upload, as it is
    # for every type, independent of this map.)
    ArtefactType.SIDECAR: [],

    # Archives - extract contents via ARCHIVE_EXTRACT (same pipeline used
    # for archives found inside disc images).  The worker detects top-level
    # artefact archives (no partition_uuid hint) and extracts them directly.
    ArtefactType.ZIP: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.TARGZ: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.RAR: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.ARC: [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.TBAFS:  [AnalysisType.ARCHIVE_EXTRACT],
    ArtefactType.XFILES: [AnalysisType.ARCHIVE_EXTRACT],

    # Acorn/RISC OS native viewable formats — convert to portable equivalents
    ArtefactType.ACORN_SPRITE: [AnalysisType.FORMAT_CONVERT],
    ArtefactType.ACORN_DRAW:   [AnalysisType.FORMAT_CONVERT],
    ArtefactType.ACORN_TEXT:   [AnalysisType.FORMAT_CONVERT],

    # Common image formats — pass through or convert to PNG/SVG
    ArtefactType.IMAGE: [AnalysisType.FORMAT_CONVERT],

    # Unknown - try to identify
    ArtefactType.UNKNOWN: [AnalysisType.FORMAT_IDENTIFY],
}


def queue_analyses_for_artefact(artefact: Artefact, hints: dict = None,
                                checksum_only: bool = False,
                                skip_duplicate_check: bool = False,
                                commit: bool = True,
                                skip_analyses: list[str] | None = None,
                                priority: int = ANALYSIS_PRIORITY_NORMAL):
    """Queue appropriate analyses for an artefact based on its type.

    CHECKSUM_COMPUTE is always prepended as the first job regardless of artefact
    type; it does not need to appear in ANALYSIS_MAP.  Pass checksum_only=True
    to skip the type-specific analyses (used when auto-analyse is off on upload).

    When called after reset_artefact_for_reanalysis, pass skip_duplicate_check=True
    to avoid redundant SELECT queries (the reset already deleted all analyses).

    Pass commit=False to defer the commit to the caller (useful for batch operations).

    skip_analyses: list of AnalysisType *names* (uppercase strings, e.g. 'FLUX_DECODE')
    to suppress.  Used when registering siblings that must not re-trigger the
    parent analysis (ping-pong prevention).

    priority: queue priority (ANALYSIS_PRIORITY_HIGH for web UI, ANALYSIS_PRIORITY_NORMAL
    for API/CLI).  Higher value = picked up sooner by workers.

    Returns the list of AnalysisType members actually queued (duplicates that
    were skipped are not included).
    """
    skip_set = set(skip_analyses or [])
    analysis_types = [AnalysisType.CHECKSUM_COMPUTE]
    if not checksum_only:
        analysis_types += ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])
    analysis_types = [t for t in analysis_types if t.name not in skip_set]
    hints_json = json.dumps(hints) if hints else None

    queued = []
    for analysis_type in analysis_types:
        if not skip_duplicate_check:
            existing = Analysis.query.filter_by(
                artefact_id=artefact.id,
                analysis_type=analysis_type
            ).filter(Analysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RUNNING])).first()
            if existing:
                continue

        analysis = Analysis(
            artefact_id=artefact.id,
            analysis_type=analysis_type,
            status=AnalysisStatus.PENDING,
            hints=hints_json,
            priority=priority,
        )
        db.session.add(analysis)
        queued.append(analysis_type)

    if commit:
        db.session.commit()
    return queued

# vim: ts=4 sw=4 et
