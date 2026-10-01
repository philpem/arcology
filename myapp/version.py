"""Version information for Arcology.

Reads the build-provided VERSION file at the repository root.
Returns 'unknown' if it is absent; never launches external programs.
"""

import os

_version_cache = None


def get_version():
    """Return a human-readable version string.

    The result is cached after the first call.
    """
    global _version_cache
    if _version_cache is None:
        _version_cache = _detect_version()
    return _version_cache


def _detect_version():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    version_file = os.path.join(repo_root, 'VERSION')
    try:
        with open(version_file) as fh:
            version = fh.read().strip()
        if version:
            return version
    except OSError:
        pass

    return 'unknown'

# vim: ts=4 sw=4 et
