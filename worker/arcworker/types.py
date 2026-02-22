"""
Type definitions for the analysis worker.

These enums must match the definitions in the main app's database.py.
"""

from enum import Enum


class ArtefactType(str, Enum):
    """Types of artefacts that can be stored and analysed."""

    # Flux-level floppy images
    SCP = "scp"                  # SuperCard Pro
    
    # Sector-level floppy images
    IMD = "imd"                  # ImageDisk
    HFE = "hfe"                  # HxC Floppy Emulator

    # Sector-level floppy or hard disc images
    RAW_SECTOR = "raw_sector"    # Raw sector image (a lot of things squash into this)
    
    # CD/DVD images
    ISO = "iso"                  # ISO 9660
    
    # Compressed raw sector images -- usually hard drives or mass-storage
    DD_ZST = "raw_sector_zst"            # Compressed with zstd
    DD_GZ = "raw_sector_gz"              # Compressed with gzip
    DD_BZ2 = "raw_sector_bz2"            # Compressed with bzip2
    
    # Documents / scans
    PDF = "pdf"
        
    # Archives (containing other artefacts)
    ZIP = "zip"
    TARGZ = "tar_gz"
    RAR = "rar"
    
    # Unknown - needs manual identification
    UNKNOWN = "unknown"



class AnalysisType(str, Enum):
    """Types of analysis that can be performed on artefacts."""

    FLUX_VISUALISATION = "flux_visualisation"
    FLUX_DECODE = "flux_decode"
    SECTOR_DUMP = "sector_dump"
    FILE_EXTRACTION = "file_extraction"
    ARCHIVE_DETECT = "archive_detect"
    ARCHIVE_EXTRACT = "archive_extract"
    METADATA_EXTRACT = "metadata_extract"
    PARTITION_DETECT = "partition_detect"
    CHECKSUM_COMPUTE = "checksum_compute"
    FORMAT_IDENTIFY = "format_identify"
