"""
Shared enum definitions for Arcology.

This module is the single source of truth for ArtefactType and AnalysisType.
It is used by both the web application (myapp/) and the analysis worker
(worker/arcworker/). Both import directly from here rather than maintaining
separate copies.
"""

import enum


class ArtefactType(enum.Enum):
    """Types of digital artefacts - auto-detected or manually specified."""
    # Flux-level floppy images
    SCP        = "scp"               # SuperCard Pro
    DFI        = "dfi"               # DiscFerret
    A2R        = "a2r"               # Applesauce A2R

    # Sector-level floppy images
    IMD        = "imd"               # ImageDisk
    HFE        = "hfe"               # HxC Floppy Emulator

    # Sector-level floppy or hard disc images
    RAW_SECTOR = "raw_sector"        # Raw sector image (a lot of things squash into this)

    # CD/DVD images
    ISO        = "iso"               # ISO 9660

    # Compressed raw sector images -- usually hard drives or mass-storage
    DD_ZST     = "raw_sector_zst"    # Compressed with zstd
    DD_GZ      = "raw_sector_gz"     # Compressed with gzip
    DD_BZ2     = "raw_sector_bz2"    # Compressed with bzip2

    # Documents / scans
    PDF        = "pdf"

    # Archives (containing other artefacts)
    ZIP        = "zip"
    TARGZ      = "tar_gz"
    RAR        = "rar"
    SEVENZ     = "7z"                # 7-Zip archive
    ARC        = "arc"               # ArcFS / Spark (RISC OS archive)
    TBAFS      = "tbafs"             # TBAFS archive (RISC OS filetype &B21)
    XFILES     = "xfiles"            # X-Files archive (RISC OS filetype &B23)

    # Acorn/RISC OS native formats (viewable/convertible)
    ACORN_SPRITE = "acorn_sprite"    # Acorn Sprite file (may contain multiple named images)
    ACORN_DRAW   = "acorn_draw"      # Acorn Draw file (vector drawing)
    ACORN_TEXT   = "acorn_text"      # Acorn text/script file (Text, Obey, Command)

    # Common image formats (raster and vector metafiles)
    IMAGE        = "image"           # JPEG, PNG, GIF, BMP, TIFF, WebP, PCX, TGA, WMF, EMF

    # Time-based media (playable in the viewer; non-native containers are
    # transcoded to MP4/M4A by MEDIA_TRANSCODE, native ones played directly)
    VIDEO        = "video"           # MP4/WebM/AVI/QuickTime/MPEG/MKV/... (container, not codec)
    AUDIO        = "audio"           # MP3/OGG/WAV/FLAC/AAC/...

    # Companion/metadata files attached to another artefact (e.g. a disk image's
    # ddrescue .map, readme, or checksum files bundled alongside it).
    SIDECAR      = "sidecar"

    # Unknown - needs manual identification
    UNKNOWN    = "unknown"


# Compressed raw-sector (whole-disk) image types — the forms a disk-image bundle
# transforms into.  Single source of truth for "is this a compressed disk image"
# so the worker detector and the web transform endpoint agree.
COMPRESSED_RAW_SECTOR_TYPES = frozenset({
    ArtefactType.DD_ZST, ArtefactType.DD_GZ, ArtefactType.DD_BZ2,
})


class AnalysisStatus(enum.Enum):
    """Status of an analysis job.

    Stored in the web database (by member NAME, like every enum column)
    and exchanged over the worker REST API (by member VALUE).  Shared so
    the worker's state checks cannot drift from the server's states.
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisType(enum.Enum):
    """Types of analysis - automatically determined by artefact type."""
    # Flux-level analyses
    FLUX_VISUALISATION     = "flux_visualisation"    # Generate flux graphs
    FLUX_DECODE            = "flux_decode"            # Attempt to decode to sectors
    DETECT_TRACK_DENSITY   = "detect_track_density"  # 40-track disc read in 80-track drive

    # Sector/filesystem analyses
    SECTOR_DUMP            = "sector_dump"            # Raw sector extraction
    FILE_EXTRACTION        = "file_extraction"        # Extract files and register listing

    # Archive/nested file analyses
    ARCHIVE_DETECT         = "archive_detect"         # Scan for archives by filetype/extension
    ARCHIVE_EXTRACT        = "archive_extract"        # Extract specific archive file

    # Metadata
    METADATA_EXTRACT       = "metadata_extract"       # Extract format metadata
    PARTITION_DETECT       = "partition_detect"       # Detect partitions (HDD/CD)

    # Verification
    CHECKSUM_COMPUTE       = "checksum_compute"       # Compute hashes
    FORMAT_IDENTIFY        = "format_identify"         # Identify file format

    # Disc image analysis
    DISC_MASTERING_DETECT  = "disc_mastering_detect"  # Mastering/duplicator fingerprint data
    DISC_PROTECTION_DETECT = "disc_protection_detect" # Copy protection signals

    # Disc security removal
    ARMLOCK_REMOVE         = "armlock_remove"         # Remove ARMlock disc security from ADFS disc images

    # Known-product recognition
    PRODUCT_RECOGNITION    = "product_recognition"    # Match extracted files against known-product definitions
    HASHDB_LINK            = "hashdb_link"             # Link extracted files against a hash database in worker-driven chunks
    HASHDB_RECOGNITION     = "hashdb_recognition"      # Backfill product recognition for a hash database
    HASHDB_DELETE          = "hashdb_delete"           # Reap a soft-deleted hash database in worker-driven chunks

    # Format conversion / viewing
    FORMAT_CONVERT         = "format_convert"         # Convert native formats to portable equivalents (Sprite→PNG, Draw→PNG/SVG, Text→UTF-8)
    MEDIA_TRANSCODE        = "media_transcode"        # Transcode non-native audio/video (AVI/QuickTime/MPEG/...) to browser-playable MP4/M4A (ffmpeg)

    # RISC OS / Acorn specific
    RISCOS_MODULE_PARSE    = "riscos_module_parse"    # Parse RISC OS relocatable module metadata (title, version, date, SWIs, commands)
    REPLAY_PROCESS         = "replay_process"         # Process Acorn Replay / ARMovie file (parse header + catalogue)
    REPLAY_TRANSCODE       = "replay_transcode"       # Transcode Acorn Replay / ARMovie video to MP4 (scotch + ffmpeg)

    # Maintenance
    HASH_RESCAN            = "hash_rescan"             # Re-link extracted files against active hash databases
    CLEANUP                = "cleanup"                 # Delete orphaned storage keys after item deletion or re-analysis
    SIMILARITY_REFRESH     = "similarity_refresh"      # Recompute one artefact's content-set similarity (task-runner, in-process)
    ITEM_DELETE            = "item_delete"             # Delete an item subtree's DB rows in batches (task-runner, in-process)
    ARTEFACT_DELETE        = "artefact_delete"         # Delete an artefact + derived subtree's DB rows in batches (task-runner)


# Control-plane / DB-only analyses.  Historically these were "worker" jobs, but
# the worker never computed anything for them: it only *drove* the job by
# looping bounded HTTP "step" endpoints, while every byte of work and every DB
# write happened in the web process.  The taskrunner container (myapp/taskrunner)
# now owns them end-to-end in-process with direct DB access, and the analysis
# worker excludes them.  This frozenset is the single source of truth for the
# split so the two consumers cannot drift.
CONTROL_PLANE_ANALYSIS_TYPES = frozenset({
    AnalysisType.HASH_RESCAN,
    AnalysisType.PRODUCT_RECOGNITION,
    AnalysisType.HASHDB_LINK,
    AnalysisType.HASHDB_DELETE,
    AnalysisType.HASHDB_RECOGNITION,
    AnalysisType.SIMILARITY_REFRESH,
    AnalysisType.ITEM_DELETE,
    AnalysisType.ARTEFACT_DELETE,
})

# Long-running ("heavy") analyses.  The fairness cap in
# ``myapp/services/analysis_queue.py`` limits how many of these may run
# concurrently so a burst of heavy jobs cannot occupy every worker and starve
# quick jobs queued behind them.  Kept deliberately conservative —
# misclassifying a type only costs throughput, never correctness.  CLEANUP is
# *excluded*: it gates the re-analysis dispatch barrier and must never be
# throttled, or an artefact's replacement analyses would stall behind it.
#
# Worker-domain types ONLY.  Control-plane analyses (ITEM_DELETE, HASHDB_LINK,
# …) are run by the single-instance taskrunner, which opts out of the cap — so
# counting a running taskrunner job against the *worker* fleet's heavy budget
# would needlessly throttle unrelated worker jobs.  They are deliberately left
# out so the cap reflects only worker load.  (Must not overlap
# CONTROL_PLANE_ANALYSIS_TYPES.)
HEAVY_ANALYSIS_TYPES = frozenset({
    AnalysisType.FILE_EXTRACTION,
    AnalysisType.ARCHIVE_EXTRACT,
    AnalysisType.REPLAY_TRANSCODE,
    AnalysisType.MEDIA_TRANSCODE,
    AnalysisType.FLUX_DECODE,
    AnalysisType.PARTITION_DETECT,
    AnalysisType.ARMLOCK_REMOVE,
})

# The fairness cap counts running HEAVY jobs against the worker fleet only, so
# the two sets must stay disjoint (see comment above).
assert not (HEAVY_ANALYSIS_TYPES & CONTROL_PLANE_ANALYSIS_TYPES), \
    "HEAVY_ANALYSIS_TYPES must not overlap CONTROL_PLANE_ANALYSIS_TYPES"

# vim: ts=4 sw=4 et
