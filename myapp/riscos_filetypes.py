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
    '3fb': 'ArcFSArc',  # ArcFS
    '68e': 'PackdDir',  # PackDir
    'a91': 'Zip',
    'b21': 'TBAFSarc',  # TBAFS
    'b23': 'X-File',    # X-Files
    'c46': 'Tar',
    'd96': 'CFSlzw',    # Computer Concepts CFS LZW
    'ddc': 'Archive',   # Spark / SparkFS
    'fc8': 'DOSDisc',
    'fca': 'Squash',    # Acorn !Squash

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
    'fb1': 'WAV',
    'fc2': 'AIFF',
    '1ad': 'AMPEG (MP2/MP3)',
    '1a8': 'Ogg Vorbis',
    '1cf': 'FLAC',
    'a8f': 'AC3',
    'f88': 'RealAudio',

    # Tracker / Module music
    '501': 'SoundTracker',
    '701': 'ProTracker',
    '7b7': 'Soundtracker',
    'acf': 'QTM',
    'c02': 'ScreamTracker',
    'c04': 'Impulse Tracker',
    'c05': 'UltraTracker',
    'cb6': 'SoundTracker',

    # Images and multimedia
    'aff': 'DrawFile',
    'd94': 'ArtWorks',
    'ae7': 'ARMovie',
    '071': 'AVI / MIDI',
    '0f8': 'MPEG',
    'a63': 'MKV (Matroska)',
    'a64': 'MP4',
    'a8d': 'MPEG VOB',
    'b9f': 'FLI/FLC',
    'bf8': 'MPEG',
    'fb2': 'AVI',
    '695': 'GIF',
    '69c': 'BMP',
    'b60': 'PNG',
    'c85': 'JPEG',
    'ff0': 'TIFF',

    # Generic RISC OS data formats
    'faf': 'HTML',
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


def resolve_default_filetype(value: str) -> str | None:
    """Resolve a user-supplied default-filetype value to canonical hex.

    Accepts a known filetype *name* ('Text', 'Data'), a known hex code
    ('fff'), or any syntactically valid 1–3 digit hex code even if it is
    not named in :data:`FILETYPE_MAP` (RISC OS defines 4096 filetypes; the
    map names only the common ones).  The result is lowercase and
    zero-padded to three digits.  Returns ``None`` if *value* is neither a
    known name nor a valid hex code.

    Examples:
        resolve_default_filetype('Text')  -> 'fff'
        resolve_default_filetype('FFF')   -> 'fff'
        resolve_default_filetype('abc')   -> 'abc'   (valid hex, unnamed)
        resolve_default_filetype('junk')  -> None
    """
    if not value:
        return None
    hexcode = lookup_filetype_hex(value)
    if hexcode:
        return hexcode
    normalised = value.strip().lower()
    if normalised and len(normalised) <= 3 and all(c in '0123456789abcdef' for c in normalised):
        return normalised.zfill(3)
    return None


def normalize_default_filetype_hint(hints: dict | None) -> dict | None:
    """Normalise the Acorn default-filetype hint in *hints* to canonical hex.

    Mutates and returns *hints* with ``HintKey.ACORN_DEFAULT_FILETYPE`` set to
    its canonical lowercase hex form.  An empty/whitespace value is dropped.
    Raises :class:`ValueError` (with a user-facing message) if the value is
    neither a known filetype name nor a valid hex code, so callers can turn it
    into a form error or a 400 response.

    Used at every ingest point (web upload/analyse forms and the REST upload
    endpoints) so the worker only ever sees a clean 3-digit hex value.
    """
    from arcology_shared.hints import HintKey

    if not hints or HintKey.ACORN_DEFAULT_FILETYPE not in hints:
        return hints
    raw = hints[HintKey.ACORN_DEFAULT_FILETYPE]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        del hints[HintKey.ACORN_DEFAULT_FILETYPE]
        return hints
    resolved = resolve_default_filetype(str(raw))
    if resolved is None:
        raise ValueError(
            f"Unrecognised Acorn filetype '{raw}'. Use a filetype name "
            f"(e.g. 'Text') or a hex code (e.g. 'fff')."
        )
    hints[HintKey.ACORN_DEFAULT_FILETYPE] = resolved
    return hints

# vim: ts=4 sw=4 et
