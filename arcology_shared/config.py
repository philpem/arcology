"""Shared configuration-value parsing helpers.

Used by the web app (myapp/utils/config.py) and the worker
(worker/arcworker/config.py).  No Flask or worker dependencies — pure Python.
"""

# Longest-first so 3-char suffixes are tried before their 1-char counterparts.
_BYTE_SUFFIXES = (
    ('tib', 1024**4), ('gib', 1024**3), ('mib', 1024**2), ('kib', 1024),
    ('t',   1024**4), ('g',   1024**3), ('m',   1024**2), ('k',   1024),
)


def parse_byte_size(value) -> int:
    """Parse a byte quantity with an optional binary suffix.

    Accepts K/KiB, M/MiB, G/GiB, T/TiB (case-insensitive, powers of 1024)
    and bare integers (as int or str).  Raises ValueError on bad input.
    """
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    for suffix, multiplier in _BYTE_SUFFIXES:
        if s.endswith(suffix):
            num = s[:-len(suffix)].strip()
            if num:
                try:
                    return int(num) * multiplier
                except ValueError:
                    pass
            break
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"Invalid byte-size value: {value!r}") from None


# The single truth table for boolean flags.  Truthy strings (case-insensitive):
# '1', 'true', 'yes'.  Every other string — including 'on'/'off' and typos — is
# false, so a misspelled value fails closed rather than silently enabling a flag.
_TRUTHY_STRINGS = ('1', 'true', 'yes')


def parse_bool(value, default: bool = False) -> bool:
    """Coerce a config/env value to bool using the shared truth table.

    ``None`` yields *default*; strings use the truthy-string set above;
    everything else is coerced with ``bool()``.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.lower() in _TRUTHY_STRINGS
    return bool(value)

# vim: ts=4 sw=4 et
