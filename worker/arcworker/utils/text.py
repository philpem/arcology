"""
Text and encoding utilities for handling filenames from various filesystems.
"""


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename for UTF-8 database storage.

    Handles filenames that may contain:
    - Surrogate escape sequences from Python's filesystem APIs
    - Acorn Latin-1 characters
    - Other non-UTF-8 encodings

    Args:
        filename: The filename to sanitize (may contain surrogates)

    Returns:
        A valid UTF-8 string safe for database storage
    """
    # Check if the string contains surrogates
    try:
        # Try to encode as UTF-8 - will fail if surrogates present
        filename.encode('utf-8')
        return filename
    except UnicodeEncodeError:
        pass

    # String contains surrogates - need to fix it
    # Convert to bytes using surrogateescape, then decode with a real encoding
    try:
        # Get the bytes that were escaped
        byte_string = filename.encode('utf-8', errors='surrogateescape')

        # Try common encodings for Acorn/retro systems
        for encoding in ['iso-8859-1', 'cp1252', 'ascii']:
            try:
                decoded = byte_string.decode(encoding)
                # Verify it's now valid UTF-8
                decoded.encode('utf-8')
                return decoded
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue

        # If all else fails, replace invalid characters
        return byte_string.decode('utf-8', errors='replace')

    except Exception:
        # Last resort: replace surrogates with replacement character
        return filename.encode('utf-8', errors='replace').decode('utf-8')


def sanitize_path(path: str) -> str:
    """
    Sanitize a file path (with directory separators) for UTF-8 database storage.

    Args:
        path: The path to sanitize (may contain surrogates in any component)

    Returns:
        A valid UTF-8 path safe for database storage
    """
    # Split path and sanitize each component
    import os
    parts = path.split(os.sep)
    sanitized_parts = [sanitize_filename(part) for part in parts]
    return os.sep.join(sanitized_parts)


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

    Returns None if:
    - The path is pure ASCII (no conversion needed)
    - Any character is outside the Latin-1 range (U+0100+, cannot round-trip)
    - The result is identical to the input (no conversion took place)
    """
    import os
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
