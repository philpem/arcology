"""Arcology - Configuration value coercion helpers.

Config keys can arrive as Python values (from myapp.cfg) or as strings (from
environment variables / .env).  bool_config() is the single truth table for
boolean flags — previously four modules each had their own parser, two of
which disagreed about what counts as true.
"""

from flask import current_app
from arcology_shared.config import parse_byte_size

__all__ = ['parse_byte_size', 'parse_bool', 'bool_config', 'int_config']

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


def bool_config(key: str, default: bool = False, app=None) -> bool:
    """Read a config key that may be a Python bool or an env-var string.

    Pass *app* when no application context is active (e.g. inside
    create_app()); otherwise current_app is used.
    """
    return parse_bool((app or current_app).config.get(key, default), default)


def int_config(key: str, default: int, app=None) -> int:
    """Read a config key that may be a Python int or an env-var string.

    Falls back to *default* when the key is missing or holds a value that
    cannot be parsed as an int, so a misconfigured value fails safe rather
    than raising at request time.

    Pass *app* when no application context is active (e.g. inside
    create_app()); otherwise current_app is used.
    """
    val = (app or current_app).config.get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

# vim: ts=4 sw=4 et
