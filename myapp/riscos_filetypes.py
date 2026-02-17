"""
RISC OS Filetype Lookup Table

Maps RISC OS filetype hex codes to human-readable names.
Sources:
- https://www.riscosopen.org/wiki/documentation/show/File%20Types
- https://en.wikipedia.org/wiki/List_of_RISC_OS_filetypes

Note: Some filetype codes may map to multiple formats as developers
sometimes picked arbitrary numbers.
"""

# Map of filetype hex code (lowercase) to name(s)
# Format: 'xxx': 'Name' or 'xxx': ['Name1', 'Name2'] for multiple
FILETYPE_MAP = {
    # Archives and Compression
    '3fb': 'ArcFSArc',
    '68e': 'PackdDir',
    'a91': 'Zip',
    'b21': 'TBAFSarc',
    'b23': 'X-File',
    'c46': 'Tar',
    'ddc': 'Archive',
    'fca': 'Squash',

    # Documents and DTP
    'b27': 'OvnPro (Ovation Pro)',
    'cdd': 'Ovation',
    'd01': 'EasiDoc (Easiwriter/Techwriter)',
    'd87': 'DocData (Impression)',
    'd88': 'Stories (Impression)',

    # Music and Sound
    'af1': 'Maestro',

    # Images and multimedia
    'aff': 'DrawFile',
    'd94': 'ArtWorks',
    'ae7': 'ARMovie',

    # Generic RISC OS data formats
    'fe1': 'Makefile',
    'fe4': 'DOS',
    'fea': 'Desktop',
    'feb': 'Obey',
    'fec': 'Template',
    'fed': 'Palette',
    'ff5': 'PostScript',
    'ff6': 'Font',
    'ff7': 'BBCFont',
    'ff8': 'Absolute',
    'ff9': 'Sprite',
    'ffa': 'Module',
    'ffb': 'BASIC',
    'ffc': 'Utility',
    'ffd': 'Data',
    'ffe': 'Command',
    'fff': 'Text',
}


def get_filetype_name(filetype_hex: str) -> str:
    """
    Get the human-readable name for a RISC OS filetype.

    Args:
        filetype_hex: Hex string (e.g., 'ddc', '3fb') - case insensitive

    Returns:
        Filetype name or comma-separated names if multiple matches,
        or empty string if not found
    """
    if not filetype_hex:
        return ''

    # Normalize to lowercase
    filetype_hex = filetype_hex.lower()

    # Look up in map
    name = FILETYPE_MAP.get(filetype_hex)

    if not name:
        return ''

    # Handle multiple names
    if isinstance(name, list):
        return ', '.join(name)

    return name


def format_filetype(filetype_hex: str) -> str:
    """
    Format filetype for display with hex code and name.

    Args:
        filetype_hex: Hex string (e.g., 'ddc', '3fb')

    Returns:
        Formatted string like "0xDDC (Archive)" or "0xDDC" if no name found
    """
    if not filetype_hex:
        return ''

    # Normalize and uppercase for display
    hex_display = f"0x{filetype_hex.upper()}"

    # Get name
    name = get_filetype_name(filetype_hex)

    if name:
        return f"{hex_display} ({name})"

    return hex_display
