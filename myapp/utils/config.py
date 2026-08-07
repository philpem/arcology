"""Arcology - Configuration value coercion helpers.

Config keys can arrive as Python values (from myapp.cfg) or as strings (from
environment variables / .env).  bool_config() is the single truth table for
boolean flags — previously four modules each had their own parser, two of
which disagreed about what counts as true.
"""

from flask import current_app
# parse_bool lives in arcology_shared so the worker (which must not import
# Flask) uses the same truth table; re-exported here for existing callers.
from arcology_shared.config import parse_bool, parse_byte_size

__all__ = ['parse_byte_size', 'parse_bool', 'bool_config', 'int_config']


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
