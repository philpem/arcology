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
    'fc8': 'DOSDisc',
    'fca': 'Squash',

    # Communications
    'cb7': '7BatCfg (ArcTerm7)',
    'cb8': '7Script (ArcTerm7)',

    # Documents and DTP
    'b27': 'OvnPro (Ovation Pro)',
    'bc5': 'ImpDoc (Impression)',
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
    '695': 'GIF',
    '69c': 'BMP',
    'b60': 'PNG',
    'c85': 'JPEG',
    'ff0': 'TIFF',

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


def lookup_filetype_hex(name_or_hex: str) -> str | None:
    """Resolve a filetype name or hex code to its canonical lowercase hex code.

    Accepts either a 3-digit hex code (e.g. 'fea', 'FEA') or a human-readable
    name (e.g. 'Desktop', 'BASIC').  Returns the lowercase hex code, or None
    if the input is not recognised.

    Examples:
        lookup_filetype_hex('fea')       -> 'fea'
        lookup_filetype_hex('Desktop')   -> 'fea'
        lookup_filetype_hex('fff')       -> 'fff'
        lookup_filetype_hex('Text')      -> 'fff'
        lookup_filetype_hex('unknown')   -> None
    """
    if not name_or_hex:
        return None

    normalised = name_or_hex.strip().lower()

    # If it looks like a hex code (1–3 hex digits) try direct lookup first.
    if normalised and all(c in '0123456789abcdef' for c in normalised):
        # Zero-pad to 3 digits so e.g. 'ff8' == 'ff8' and '3fb' == '3fb'
        candidate = normalised.zfill(3)
        if candidate in FILETYPE_MAP:
            return candidate

    # Otherwise try a case-insensitive reverse lookup by name.
    for hex_code, names in FILETYPE_MAP.items():
        if isinstance(names, list):
            name_list = names
        else:
            name_list = [names]
        for n in name_list:
            if n.lower() == normalised:
                return hex_code

    return None

# vim: ts=4 sw=4 et
