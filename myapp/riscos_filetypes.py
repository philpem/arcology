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
    'a91': 'Zip',
    'c46': 'ArcFS',
    'ddc': 'Archive',
    'f89': 'PackDir',
    '68e': 'ArcFS+',

    # Documents
    'aff': 'Draw',
    'b60': 'PNG',
    'c85': 'JPEG',
    'fff': 'Text',
    '1ad': 'WordPerfect',
    'f8c': 'MessageTrans',
    'aca': 'PDF',

    # Spreadsheets and Databases
    'db0': 'Lotus123',
    'dfe': 'CSV',
    '1d3': ['DBaseII', 'ParadoxDB'],
    'db2': 'DBaseII',
    'db3': 'DBaseIII',
    'dbe': 'DBaseIV',

    # Programming
    'ffd': 'Data',
    'ffe': 'Command',
    'feb': 'Obey',
    'fea': 'Desktop',
    'faf': 'HTML',
    'f81': 'Squash',
    '102': 'Sprite',
    'ff9': 'Sprite',
    'fae': ['TeX', 'LaTeX'],
    'ce5': 'Makefile',
    'ddc': 'Archive',

    # Music and Sound
    'cb6': 'Maestro',
    'f96': 'SoundPro',
    'fb1': 'WaveForm',
    '1ad': 'MIDI',
    'd3c': 'Tracker',

    # Images
    '695': 'GIF',
    'b60': 'PNG',
    'c85': 'JPEG',
    'ff0': 'BBC',
    'aff': 'DrawFile',
    '69c': 'SVG',
    'afd': 'ArtWorks',

    # BASIC and Source
    'ffb': 'BASIC',
    'fff': ['Text', 'Source'],
    'f79': 'ARMovie',

    # System
    'ffd': 'Data',
    'ffc': 'Utility',
    'ffb': 'BASIC',
    'ffa': 'Module',
    'ff9': 'Sprite',
    'ff8': 'Absolute',

    # Applications
    '2000': 'Application',

    # Fonts
    'f00': 'BBCFont',
    'f0b': 'IntMetric',
    'f0c': 'IntFont',
    'f0d': 'OutlFont',
    'f0e': 'OutlMetr',
    'ffd': 'FontCache',
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
