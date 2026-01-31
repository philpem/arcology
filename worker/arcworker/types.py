"""
Type definitions for the analysis worker.

These enums must match the definitions in the main app's database.py.
"""

from enum import Enum


class ArtefactType(str, Enum):
    """Types of artefacts that can be stored and analysed."""

    # Flux-level
    SCP = "scp"
    KF = "kf"
    IPF = "ipf"
    FLUX_RAW = "flux_raw"

    # Sector-level floppy
    IMD = "imd"
    TD0 = "td0"
    D64 = "d64"
    ADF = "adf"
    DSK = "dsk"
    IMG = "img"
    HFE = "hfe"

    # CD/DVD
    ISO = "iso"
    BIN_CUE = "bin_cue"

    # Hard drive
    HDD_RAW = "hdd_raw"

    # Documents/images
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"

    # Archives
    ZIP = "zip"
    TARGZ = "tar_gz"

    UNKNOWN = "unknown"


class AnalysisType(str, Enum):
    """Types of analysis that can be performed on artefacts."""

    FLUX_VISUALISATION = "flux_visualisation"
    FLUX_DECODE = "flux_decode"
    SECTOR_DUMP = "sector_dump"
    FILE_LISTING = "file_listing"
    FILE_EXTRACTION = "file_extraction"
    METADATA_EXTRACT = "metadata_extract"
    PARTITION_DETECT = "partition_detect"
    CHECKSUM_COMPUTE = "checksum_compute"
    FORMAT_IDENTIFY = "format_identify"
