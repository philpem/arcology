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

    # RISC OS / Acorn specific
    RISCOS_MODULE_PARSE    = "riscos_module_parse"    # Parse RISC OS relocatable module metadata (title, version, date, SWIs, commands)
    REPLAY_PROCESS         = "replay_process"         # Process Acorn Replay / ARMovie file (parse header + catalogue)
    REPLAY_TRANSCODE       = "replay_transcode"       # Transcode Acorn Replay / ARMovie video to MP4 (scotch + ffmpeg)

    # Explicit-content moderation
    NSFW_SCAN              = "nsfw_scan"              # Two-stage ONNX explicit-content image classification

    # Maintenance
    HASH_RESCAN            = "hash_rescan"             # Re-link extracted files against active hash databases
    CLEANUP                = "cleanup"                 # Delete orphaned storage keys after item deletion or re-analysis

# vim: ts=4 sw=4 et
