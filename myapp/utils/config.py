"""Arcology - Configuration value coercion helpers.

Config keys can arrive as Python values (from myapp.cfg) or as strings (from
environment variables / .env).  bool_config() is the single truth table for
boolean flags — previously four modules each had their own parser, two of
which disagreed about what counts as true.
"""

from flask import current_app


def bool_config(key: str, default: bool = False, app=None) -> bool:
    """Read a config key that may be a Python bool or an env-var string.

    Truthy strings (case-insensitive): '1', 'true', 'yes'.  Every other
    string — including 'on'/'off' and typos — is false, so a misspelled
    value fails closed rather than silently enabling a flag.  Non-string
    values are coerced with bool().

    Pass *app* when no application context is active (e.g. inside
    create_app()); otherwise current_app is used.
    """
    val = (app or current_app).config.get(key, default)
    if isinstance(val, str):
        return val.lower() in ('1', 'true', 'yes')
    return bool(val)

# vim: ts=4 sw=4 et
