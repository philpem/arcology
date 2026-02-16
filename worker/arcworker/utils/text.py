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
