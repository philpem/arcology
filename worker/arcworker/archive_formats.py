"""
Centralized archive format definitions.

This module defines all supported archive and compression formats for Arcology.
Used by both the web application and worker to ensure consistent handling.
"""

from enum import Enum


class ArchiveType(Enum):
    """
    Types of archives and compressed files.

    Note: Some formats are single-file compressors (COMPRESS type),
    while others are multi-file archivers (ARCHIVE type).
    """
    # RISC OS Archives (multi-file)
    ARCFS = "arcfs"          # ArcFS archive (filetype &3FB)
    SPARK = "spark"          # Spark archive (filetype &DDC)
    ZIP_RISCOS = "zip_riscos"    # ZIP archive with RISC OS filetypes (filetype &A91, or &DDC fallback)
    PACKDIR = "packdir"      # PackDir archive (filetype &68E)
    TBAFS = "tbafs"          # TBAFS archive (filetype &B21)
    XFILES = "xfiles"        # X-Files archive (filetype &B23)

    # RISC OS Single-file compressors
    CFS = "cfs"              # Computer Concepts CFS (filetype &D96)
    SQUASH = "squash"        # Squash compressed file (filetype &FCA)

    # RISC OS Disk images (nested)
    FCFS = "fcfs"            # Filecore hard disk image (filetype &FCD)
    DOSDISC = "dosdisc"      # PC hard disk image (filetype &FC8)

    # PC Archives (multi-file)
    ZIP = "zip"
    RAR = "rar"
    TAR = "tar"
    TARGZ = "tar_gz"
    TARBZ2 = "tar_bz2"
    TARXZ = "tar_xz"
    SEVENZ = "7z"

    # PC Single-file compressors
    GZIP = "gzip"            # .gz files
    BZIP2 = "bzip2"          # .bz2 files
    XZ = "xz"                # .xz files
    ZSTD = "zstd"            # .zst files


class ArchiveCategory(Enum):
    """Category of archive format."""
    ARCHIVE = "archive"          # Multi-file archive (creates directory)
    COMPRESS = "compress"        # Single-file compressor (creates single file)
    DISK_IMAGE = "disk_image"    # Nested disk image (requires special handling)


# Archive format definitions
ARCHIVE_FORMATS = {
    # RISC OS Archives
    ArchiveType.ARCFS: {
        'name': 'ArcFS Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': '3fb',
        'extensions': ['.arc'],
        'tool': 'riscosarc',
        'description': 'ArcFS archive format',
        'extract_creates_dir': True,
    },
    ArchiveType.SPARK: {
        'name': 'Spark Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': 'ddc',
        'extensions': ['.spk', '.spark'],
        'tool': 'riscosarc',
        'description': 'Spark archive format (PKZIP-like)',
        'extract_creates_dir': True,
    },
    ArchiveType.ZIP_RISCOS: {
        'name': 'ZIP (RISC OS)',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': 'a91',
        'extensions': [],
        'tool': 'unzip',
        'description': 'ZIP archive containing RISC OS files with ,xxx filetype suffixes',
        'extract_creates_dir': True,
    },
    ArchiveType.PACKDIR: {
        'name': 'PackDir',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': '68e',
        'extensions': [],
        'tool': 'riscosarc',
        'description': 'PackDir archive (directory packed to single file)',
        'extract_creates_dir': True,
    },
    ArchiveType.TBAFS: {
        'name': 'TBAFS Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': 'b21',
        'extensions': [],
        'tool': 'tbafs-extractor',
        'description': 'TBAFS archive format',
        'extract_creates_dir': True,
    },
    ArchiveType.XFILES: {
        'name': 'X-Files Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': 'b23',
        'extensions': [],
        'tool': 'custom',
        'description': 'X-Files archive with long filenames',
        'extract_creates_dir': True,
    },

    # RISC OS Compressors (single-file)
    ArchiveType.CFS: {
        'name': 'CFS Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': 'd96',
        'extensions': [],
        'tool': 'riscosarc',
        'description': 'Computer Concepts CFS - decompresses to single file with same name',
        'extract_creates_dir': False,
        'output_filename': 'same_as_input',  # Decompresses to file with same name
    },
    ArchiveType.SQUASH: {
        'name': 'Squash Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': 'fca',
        'extensions': [],
        'tool': 'riscosarc',
        'description': 'Squash compressed file - decompresses to single file with same name',
        'extract_creates_dir': False,
        'output_filename': 'same_as_input',
    },

    # RISC OS Disk Images
    ArchiveType.FCFS: {
        'name': 'FCFS Hard Disk Image',
        'category': ArchiveCategory.DISK_IMAGE,
        'risc_os_filetype': 'fcd',
        'extensions': ['.fcfs'],
        'tool': 'fcfs2raw',
        'description': 'Filecore hard disk image - convert to raw then extract as ADFS',
        'extract_creates_dir': True,
        'requires_conversion': True,
    },
    ArchiveType.DOSDISC: {
        'name': 'DOS Disc Image',
        'category': ArchiveCategory.DISK_IMAGE,
        'risc_os_filetype': 'fc8',
        'extensions': [],
        'tool': 'sfdisk+7z',
        'description': 'PC hard disk image file - extract as DOS/FAT filesystem',
        'extract_creates_dir': True,
    },

    # PC Archives
    ArchiveType.ZIP: {
        'name': 'ZIP Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.zip'],
        'tool': 'unzip',
        'description': 'ZIP archive',
        'extract_creates_dir': True,
    },
    ArchiveType.RAR: {
        'name': 'RAR Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.rar'],
        'tool': 'unrar',
        'description': 'RAR archive',
        'extract_creates_dir': True,
    },
    ArchiveType.TAR: {
        'name': 'TAR Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.tar'],
        'tool': 'tar',
        'description': 'TAR archive (uncompressed)',
        'extract_creates_dir': True,
    },
    ArchiveType.TARGZ: {
        'name': 'TAR.GZ Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.tar.gz', '.tgz'],
        'tool': 'tar',
        'description': 'TAR archive compressed with gzip',
        'extract_creates_dir': True,
    },
    ArchiveType.TARBZ2: {
        'name': 'TAR.BZ2 Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.tar.bz2', '.tbz2'],
        'tool': 'tar',
        'description': 'TAR archive compressed with bzip2',
        'extract_creates_dir': True,
    },
    ArchiveType.TARXZ: {
        'name': 'TAR.XZ Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.tar.xz', '.txz'],
        'tool': 'tar',
        'description': 'TAR archive compressed with xz',
        'extract_creates_dir': True,
    },
    ArchiveType.SEVENZ: {
        'name': '7-Zip Archive',
        'category': ArchiveCategory.ARCHIVE,
        'risc_os_filetype': None,
        'extensions': ['.7z'],
        'tool': '7z',
        'description': '7-Zip archive',
        'extract_creates_dir': True,
    },

    # PC Single-file compressors
    ArchiveType.GZIP: {
        'name': 'GZIP Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': None,
        'extensions': ['.gz'],
        'tool': 'gzip',
        'description': 'GZIP compressed file',
        'extract_creates_dir': False,
        'output_filename': 'strip_extension',  # file.txt.gz -> file.txt
    },
    ArchiveType.BZIP2: {
        'name': 'BZIP2 Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': None,
        'extensions': ['.bz2'],
        'tool': 'bzip2',
        'description': 'BZIP2 compressed file',
        'extract_creates_dir': False,
        'output_filename': 'strip_extension',
    },
    ArchiveType.XZ: {
        'name': 'XZ Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': None,
        'extensions': ['.xz'],
        'tool': 'xz',
        'description': 'XZ compressed file',
        'extract_creates_dir': False,
        'output_filename': 'strip_extension',
    },
    ArchiveType.ZSTD: {
        'name': 'ZSTD Compressed File',
        'category': ArchiveCategory.COMPRESS,
        'risc_os_filetype': None,
        'extensions': ['.zst'],
        'tool': 'zstd',
        'description': 'Zstandard compressed file',
        'extract_creates_dir': False,
        'output_filename': 'strip_extension',
    },
}


# Reverse mappings for quick lookup
FILETYPE_TO_ARCHIVE = {
    info['risc_os_filetype']: archive_type
    for archive_type, info in ARCHIVE_FORMATS.items()
    if info['risc_os_filetype'] is not None
}

EXTENSION_TO_ARCHIVE = {}
for archive_type, info in ARCHIVE_FORMATS.items():
    for ext in info['extensions']:
        EXTENSION_TO_ARCHIVE[ext.lower()] = archive_type


def get_archive_info(archive_type: ArchiveType) -> dict:
    """Get information about an archive format."""
    return ARCHIVE_FORMATS.get(archive_type, {})


def get_archive_by_filetype(filetype: str) -> ArchiveType:
    """Get archive type from RISC OS filetype (e.g., '3fb' -> ArchiveType.ARCFS)."""
    return FILETYPE_TO_ARCHIVE.get(filetype.lower())


def get_archive_by_extension(filename: str) -> ArchiveType:
    """Get archive type from file extension."""
    filename_lower = filename.lower()

    # Check multi-part extensions first (e.g., .tar.gz)
    for ext in ['.tar.gz', '.tar.bz2', '.tar.xz']:
        if filename_lower.endswith(ext):
            return EXTENSION_TO_ARCHIVE.get(ext)

    # Check single extensions
    for ext, archive_type in EXTENSION_TO_ARCHIVE.items():
        if filename_lower.endswith(ext):
            return archive_type

    return None


def is_archive_filetype(filetype: str) -> bool:
    """Check if RISC OS filetype corresponds to a known archive."""
    return filetype and filetype.lower() in FILETYPE_TO_ARCHIVE


def is_archive_format(archive_type: ArchiveType) -> bool:
    """Check if archive type is a multi-file archive (vs single-file compressor)."""
    info = ARCHIVE_FORMATS.get(archive_type, {})
    return info.get('category') == ArchiveCategory.ARCHIVE


def is_compressor_format(archive_type: ArchiveType) -> bool:
    """Check if archive type is a single-file compressor."""
    info = ARCHIVE_FORMATS.get(archive_type, {})
    return info.get('category') == ArchiveCategory.COMPRESS


def is_disk_image_format(archive_type: ArchiveType) -> bool:
    """Check if archive type is actually a nested disk image."""
    info = ARCHIVE_FORMATS.get(archive_type, {})
    return info.get('category') == ArchiveCategory.DISK_IMAGE
