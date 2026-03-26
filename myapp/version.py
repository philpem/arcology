"""Version information for Arcology.

Detects version from git at startup; falls back to a VERSION file
at the repository root; returns 'unknown' if neither is available.
Git does not need to be installed for the application to run.
"""

import os
import subprocess

_version_cache = None


def get_version():
    """Return a human-readable version string.

    The result is cached after the first call so that git is not
    invoked on every request.
    """
    global _version_cache
    if _version_cache is None:
        _version_cache = _detect_version()
    return _version_cache


def _detect_version():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Try git describe (works in development; not required at runtime)
    try:
        result = subprocess.run(
            ['git', 'describe', '--tags', '--always', '--long'],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=repo_root,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            # "v1.2.3-5-gabcdef1" -> tag + distance + hash
            # plain "abcdef1"       -> no tags, just the commit hash
            parts = raw.rsplit('-', 2)
            if len(parts) == 3 and parts[2].startswith('g'):
                tag, distance, ghash = parts
                commit = ghash[1:]           # strip leading 'g'
                if distance == '0':
                    return f'{tag} (commit {commit})'
                return f'{tag}+{distance} (commit {commit})'
            # No tags — raw is the abbreviated commit hash
            return f'dev (commit {raw})'
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fall back to a VERSION file shipped with the release
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
