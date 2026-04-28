"""
Text and encoding utilities for handling filenames from various filesystems.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RISC OS Latin1 character set
# ---------------------------------------------------------------------------
#
# RISC OS Latin1 is the default 8-bit alphabet used on Acorn computers.  It
# agrees with ISO 8859-1 (and therefore Unicode) for bytes 0x00–0x7F (ASCII)
# and 0xA0–0xFF (Latin-1 Supplement).  The C1 range 0x80–0x9F diverges: where
# ISO 8859-1 defines control codes, RISC OS assigns printable characters.
#
# Notable entries:
#   0x81/0x82  Ŵŵ   — Welsh capital/small W with circumflex
#   0x85/0x86  Ŷŷ   — Welsh capital/small Y with circumflex
#   0x83  ◰ (U+25F0) — Window-resize icon; no direct Unicode equivalent, this
#                      is the nearest geometric approximation (Wikipedia)
#   0x84  🯀 (U+1FBC0) — Window-close icon; encoded in the Unicode "Symbols for
#                       Legacy Computing" block (added Unicode 13.0, 2020)
#   0x87  (U+E087)   — RISC OS "87 glyph" (subscript-8 superscript-7 ligature,
#                      not proposed for Unicode); mapped to a Private Use Area
#                      code point to preserve bijectivity
#   0x88–0x8B        — Scroll-bar bubble arrows; approximated with standard
#                      Unicode directional arrows
#
# The mapping is bijective: every code point in the table is distinct and lies
# outside both the ASCII range (U+0000–U+007F) and the Latin-1 Supplement
# (U+00A0–U+00FF), so no collision can arise between the three ranges.
#
# References:
#   https://en.wikipedia.org/wiki/RISC_OS_character_set
#   https://github.com/gerph/python-codecs-riscos

_RISCOS_C1: dict[int, str] = {
    0x80: '\u20ac',      # €   Euro Sign
    0x81: '\u0174',      # Ŵ   Latin Capital Letter W with Circumflex (Welsh)
    0x82: '\u0175',      # ŵ   Latin Small Letter W with Circumflex (Welsh)
    0x83: '\u25f0',      # ◰   White Square with Upper Left Quadrant (resize icon)
    0x84: '\U0001fbc0',  # 🯀  White Heavy Saltire with Rounded Corners (close icon)
    0x85: '\u0176',      # Ŷ   Latin Capital Letter Y with Circumflex (Welsh)
    0x86: '\u0177',      # ŷ   Latin Small Letter Y with Circumflex (Welsh)
    0x87: '\ue087',      # (PUA) RISC OS "87 glyph" — no Unicode equivalent
    0x88: '\u2190',      # ←   Leftwards Arrow (left scroll bubble)
    0x89: '\u2192',      # →   Rightwards Arrow (right scroll bubble)
    0x8a: '\u2191',      # ↑   Upwards Arrow (up scroll bubble)
    0x8b: '\u2193',      # ↓   Downwards Arrow (down scroll bubble)
    0x8c: '\u2026',      # …   Horizontal Ellipsis
    0x8d: '\u2122',      # ™   Trade Mark Sign
    0x8e: '\u2030',      # ‰   Per Mille Sign
    0x8f: '\u2022',      # •   Bullet
    0x90: '\u2018',      # '   Left Single Quotation Mark
    0x91: '\u2019',      # '   Right Single Quotation Mark
    0x92: '\u2039',      # ‹   Single Left-Pointing Angle Quotation Mark
    0x93: '\u203a',      # ›   Single Right-Pointing Angle Quotation Mark
    0x94: '\u201c',      # "   Left Double Quotation Mark
    0x95: '\u201d',      # "   Right Double Quotation Mark
    0x96: '\u201e',      # „   Double Low-9 Quotation Mark
    0x97: '\u2013',      # –   En Dash
    0x98: '\u2014',      # —   Em Dash
    0x99: '\u2212',      # −   Minus Sign (distinct from HYPHEN-MINUS U+002D)
    0x9a: '\u0152',      # Œ   Latin Capital Ligature OE
    0x9b: '\u0153',      # œ   Latin Small Ligature OE
    0x9c: '\u2020',      # †   Dagger
    0x9d: '\u2021',      # ‡   Double Dagger
    0x9e: '\ufb01',      # ﬁ   Latin Small Ligature FI
    0x9f: '\ufb02',      # ﬂ   Latin Small Ligature FL
}

# Inverse: Unicode character → RISC OS byte, for encoding back to RISC OS Latin1.
_RISCOS_C1_INVERSE: dict[str, int] = {v: k for k, v in _RISCOS_C1.items()}


def decode_riscos_latin1(data: bytes) -> str:
    """
    Decode a byte string using the RISC OS Latin1 character set.

    Bytes 0x00–0x7F and 0xA0–0xFF map to the same Unicode code point (identical
    to ISO 8859-1).  Bytes 0x80–0x9F are remapped via _RISCOS_C1.

    This function always succeeds: every possible byte value has a defined
    Unicode mapping.
    """
    chars = []
    for b in data:
        if b < 0x80 or b >= 0xa0:
            # ASCII range and Latin-1 Supplement: code point equals byte value
            chars.append(chr(b))
        else:
            chars.append(_RISCOS_C1[b])
    return ''.join(chars)


def encode_riscos_latin1(text: str) -> bytes:
    """
    Encode a Unicode string back to RISC OS Latin1 bytes.

    Raises UnicodeEncodeError for any character that has no representation in
    the RISC OS Latin1 character set.
    """
    result = bytearray()
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp < 0x80 or (0xa0 <= cp <= 0xff):
            result.append(cp)
        elif ch in _RISCOS_C1_INVERSE:
            result.append(_RISCOS_C1_INVERSE[ch])
        else:
            raise UnicodeEncodeError(
                'riscos-latin1', text, i, i + 1,
                f'character {ch!r} (U+{cp:04X}) is not representable in RISC OS Latin1'
            )
    return bytes(result)


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename for UTF-8 database storage.

    Handles filenames that may contain surrogate escape sequences produced by
    Python's filesystem APIs (PEP 383) when the underlying bytes are not valid
    UTF-8 — as occurs with Acorn Latin-1 filenames on a UTF-8 Linux system.

    The primary decode is RISC OS Latin1, which correctly handles the full
    byte range including the 0x80–0x9F characters unique to RISC OS.  A plain
    UTF-8 replace fallback is kept for any edge case that bypasses the main
    path.

    Args:
        filename: The filename to sanitize (may contain surrogates).

    Returns:
        A valid UTF-8 string safe for database storage.
    """
    # PostgreSQL forbids NUL characters in string columns.
    filename = filename.replace('\x00', '')
    if not filename:
        return filename

    # Fast path: already valid UTF-8, nothing to do.
    try:
        filename.encode('utf-8')
        return filename
    except UnicodeEncodeError:
        pass

    # Recover the original raw bytes from the surrogate-escaped string, then
    # decode with RISC OS Latin1.
    try:
        raw = filename.encode('utf-8', errors='surrogateescape')
        return decode_riscos_latin1(raw)
    except Exception:
        pass

    # Last resort: replace surrogates with the Unicode replacement character.
    return filename.encode('utf-8', errors='replace').decode('utf-8')


def sanitize_path(path: str) -> str:
    """
    Sanitize a file path (with directory separators) for UTF-8 database storage.

    Args:
        path: The path to sanitize (may contain surrogates in any component).

    Returns:
        A valid UTF-8 path safe for database storage.
    """
    parts = path.split(os.sep)
    return os.sep.join(sanitize_filename(part) for part in parts)


# ---------------------------------------------------------------------------
# Post-extraction filename normalisation
# ---------------------------------------------------------------------------

def normalize_extracted_filenames(
    root: Path,
    decoder: Callable[[bytes], str] = decode_riscos_latin1,
) -> None:
    """
    Rename files and directories under *root* whose names contain raw non-ASCII
    bytes (represented by Python as surrogate-escaped characters) to their
    correct Unicode equivalents.

    Call this immediately after any extraction tool finishes writing to an
    output directory, before enumerate_extracted_files() or before any path
    inside the directory is passed to a subprocess.  After normalisation every
    name in the tree is valid UTF-8 and can be stored in the database or handed
    to external tools without special encoding workarounds.

    The *decoder* argument determines how raw bytes are mapped to Unicode.  The
    default (decode_riscos_latin1) is correct for Acorn/RISC OS content.  Pass
    a different callable when processing filesystems with a different native
    encoding (e.g. CP437 for MS-DOS).

    Renames are performed bottom-up so that a directory's contents are all
    renamed before the directory entry itself is touched.  On collision (the
    target Unicode name already exists in the same directory) the entry is left
    with its original raw-byte name and a warning is logged — this should be
    extremely rare in practice.
    """
    import logging
    log = logging.getLogger(__name__)

    for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
        dir_path = Path(dirpath)
        # Process files then subdirectory entries (both may need renaming).
        for name in filenames + dirnames:
            try:
                name.encode('utf-8')
                continue  # Already valid UTF-8; nothing to do.
            except UnicodeEncodeError:
                pass

            # Recover raw bytes from the surrogate-escaped name, then decode.
            raw = name.encode('utf-8', errors='surrogateescape')
            unicode_name = decoder(raw)

            # Path with surrogate escapes — Python's os layer handles encoding
            # transparently via surrogateescape when making the syscall.
            old_path = dir_path / name
            new_path = dir_path / unicode_name

            if new_path.exists():
                log.warning(
                    'normalize_extracted_filenames: skipping %r → %r: target exists',
                    str(old_path), str(new_path),
                )
                continue

            try:
                old_path.rename(new_path)
            except OSError as exc:
                log.warning(
                    'normalize_extracted_filenames: could not rename %r → %r: %s',
                    str(old_path), str(new_path), exc,
                )


# ---------------------------------------------------------------------------
# Post-extraction RISC OS C1 fixup for ZIP archives
# ---------------------------------------------------------------------------

def fix_riscos_c1_filenames(root: Path) -> None:
    """
    Rename files and directories under *root* whose names contain ISO-8859-1
    C1 control characters (U+0080–U+009F) to their RISC OS Latin-1 equivalents.

    When unzip extracts a RISC OS ZIP archive with '-O iso-8859-1', bytes in
    the range 0x80–0x9F are decoded as ISO-8859-1 C1 control codes (U+0080–
    U+009F) rather than the RISC OS printable characters they represent.  This
    function remaps those code points using the same _RISCOS_C1 table used by
    decode_riscos_latin1(), restoring the correct Unicode characters
    (e.g. 0x80 → €, 0x8C → …, 0x8D → ™).

    C1 control codes never appear legitimately in filenames, so any occurrence
    reliably indicates a misinterpreted RISC OS byte.

    Renames are bottom-up (directory contents before the directory itself) to
    avoid touching a parent directory before its children have been processed.
    """
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
        dir_path = Path(dirpath)
        for name in filenames + dirnames:
            if not any(0x80 <= ord(c) <= 0x9F for c in name):
                continue

            new_name = ''.join(
                _RISCOS_C1[ord(c)] if 0x80 <= ord(c) <= 0x9F else c
                for c in name
            )
            if new_name == name:
                continue

            old_path = dir_path / name
            new_path = dir_path / new_name
            if new_path.exists():
                log.warning(
                    'fix_riscos_c1_filenames: skipping %r → %r: target exists',
                    str(old_path), str(new_path),
                )
                continue
            try:
                old_path.rename(new_path)
            except OSError as exc:
                log.warning(
                    'fix_riscos_c1_filenames: could not rename %r → %r: %s',
                    str(old_path), str(new_path), exc,
                )


# ---------------------------------------------------------------------------
# Backward-compatibility helper for pre-normalisation analyses
# ---------------------------------------------------------------------------

def make_latin1_fspath(path: str) -> str | None:
    """
    Create a filesystem-compatible path for paths that contain Unicode characters
    originally decoded from Latin-1 bytes (e.g. Acorn hard space 0xA0).

    sanitize_path() converts surrogate-escaped bytes (e.g. \\udca0) to proper
    Unicode (e.g. U+00A0 NON-BREAKING SPACE) for database storage.  When the
    path is later used to locate the file on disk, Python would encode U+00A0
    as two UTF-8 bytes (0xC2 0xA0), but the actual file contains the single raw
    byte (0xA0).  This mismatch causes exists() to return False.

    This function re-encodes the Unicode path to Latin-1 bytes and then uses
    os.fsdecode() (which applies the surrogateescape error handler) to produce
    a surrogate-escaped string that Python will correctly map back to the raw
    single-byte sequence when making filesystem calls.

    NOTE: This workaround is only needed for analyses run before
    normalize_extracted_filenames() was introduced.  For new analyses all
    extracted files are renamed to their Unicode equivalents at extraction time,
    so the database path and the on-disk name agree without any conversion.

    Returns None if:
    - The path is pure ASCII (no conversion needed)
    - Any character is outside the Latin-1 range (U+0100+, cannot round-trip)
    - The result is identical to the input (no conversion took place)
    """
    if all(ord(c) < 128 for c in path):
        return None  # All ASCII, no conversion needed
    try:
        # Encode to Latin-1 bytes; raises UnicodeEncodeError for chars > U+00FF
        path_bytes = path.encode('latin-1')
        # Decode using the filesystem encoding with surrogateescape so Python
        # maps each non-ASCII byte back to its surrogate counterpart
        fspath = os.fsdecode(path_bytes)
        if fspath != path:
            return fspath
    except (UnicodeEncodeError, ValueError):
        pass
    return None

# vim: ts=4 sw=4 et
