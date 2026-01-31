"""
Base utilities for running external tools.
"""

import hashlib
import subprocess
from pathlib import Path

from ..config import log


def run_tool(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    """
    Run a tool command with logging.

    Args:
        cmd: Command and arguments to run
        timeout: Maximum execution time in seconds

    Returns:
        CompletedProcess with stdout, stderr, and returncode
    """
    log.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout
    )
    if result.returncode != 0:
        log.warning(f"Tool returned {result.returncode}: {result.stderr.decode()[:500]}")
    return result


def compute_file_hash(filepath: Path) -> tuple[str, str, int]:
    """
    Compute MD5, SHA256 and file size.

    Args:
        filepath: Path to the file to hash

    Returns:
        Tuple of (md5_hex, sha256_hex, file_size)
    """
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0

    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)

    return md5.hexdigest(), sha256.hexdigest(), size
