"""
Artefact-type registry shared by the web app, worker, and CLI.

  EXTENSION_MAP          — filename extension → ArtefactType (single source
                           of truth for upload-time type detection)
  COMPRESSOR_SUFFIXES    — compression suffixes recognised on raw-sector
                           images (``drive.dd.zst`` …)
  RAW_SECTOR_EXTENSIONS  — extensions that map to ArtefactType.RAW_SECTOR
  ARCHIVE_EXTENSIONS     — extensions that map to an archive ArtefactType
  detect_artefact_type() — extension-based type detection

Server-side analysis scheduling (ANALYSIS_MAP, queue_analyses_for_artefact)
stays in ``myapp/services/artefact_types.py`` — it depends on the database
layer, which the worker and CLI must not import.
"""

import os
from .enums import ArtefactType

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

# Compressor suffixes recognised on top of a raw-sector extension, in order
# of preference when several compressed forms of the same image exist.
COMPRESSOR_SUFFIXES = ('.zst', '.gz', '.bz2')

# Archive container artefact types (extract via ARCHIVE_EXTRACT).
ARCHIVE_ARTEFACT_TYPES = frozenset({
    ArtefactType.ZIP, ArtefactType.TARGZ, ArtefactType.RAR,
    ArtefactType.ARC, ArtefactType.TBAFS, ArtefactType.XFILES,
})

# Derived extension sets, for callers that classify by category rather than
# exact type (e.g. the bulk-import duplicate-form ranking).
RAW_SECTOR_EXTENSIONS = frozenset(
    ext for ext, atype in EXTENSION_MAP.items()
    if atype is ArtefactType.RAW_SECTOR
)
ARCHIVE_EXTENSIONS = frozenset(
    ext for ext, atype in EXTENSION_MAP.items()
    if atype in ARCHIVE_ARTEFACT_TYPES
)


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
    for suffix in COMPRESSOR_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    _, ext = os.path.splitext(stem)
    return EXTENSION_MAP.get(ext, ArtefactType.UNKNOWN)

# vim: ts=4 sw=4 et
