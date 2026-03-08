"""
Base utilities for running external tools.
"""

import hashlib
import subprocess
import time
from pathlib import Path

from ..config import log, TOOL_TIMEOUT
from ..utils.text import sanitize_filename


def run_tool(cmd: list[str], timeout: int = None, cwd: str = None) -> subprocess.CompletedProcess:
    """
    Run a tool command with logging.

    Args:
        cmd: Command and arguments to run
        timeout: Maximum execution time in seconds
        cwd: Working directory for the command (optional)

    Returns:
        CompletedProcess with stdout, stderr, and returncode
    """
    if timeout is None:
        timeout = TOOL_TIMEOUT
    log.debug(f"Running: {' '.join(cmd)}" + (f" (cwd={cwd})" if cwd else ""))
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        cwd=cwd
    )
    if result.returncode != 0:
        log.warning(f"Tool returned {result.returncode}: {result.stderr.decode(errors='replace')[:500]}")
    return result


def get_process_output(result: subprocess.CompletedProcess, cmd: list[str],
                       duration: float | None = None, max_output_len: int = 50000) -> dict:
    """
    Extract process output information from a CompletedProcess for storage.

    Args:
        result: The CompletedProcess object from subprocess.run()
        cmd: The command that was executed
        duration: Execution time in seconds (optional)
        max_output_len: Maximum length to store for stdout/stderr (default 50KB)

    Returns:
        Dict containing command, returncode, stdout, stderr, and duration
    """
    stdout = result.stdout.decode(errors='replace')
    stderr = result.stderr.decode(errors='replace')

    # Sanitize the command string: paths containing raw Latin-1 bytes (e.g.
    # Acorn hard space 0xA0) are represented in Python as surrogate-escaped
    # characters (\udca0).  These cannot be UTF-8 encoded and would cause
    # UnicodeEncodeError when the stored JSON is later rendered in the web UI.
    # Convert them to their Unicode equivalents (e.g. U+00A0) for safe storage.
    command_str = sanitize_filename(' '.join(cmd))

    # Truncate if too long (keep first and last portions)
    if len(stdout) > max_output_len:
        half = max_output_len // 2
        stdout = stdout[:half] + f"\n\n... [truncated {len(stdout) - max_output_len} bytes] ...\n\n" + stdout[-half:]
    if len(stderr) > max_output_len:
        half = max_output_len // 2
        stderr = stderr[:half] + f"\n\n... [truncated {len(stderr) - max_output_len} bytes] ...\n\n" + stderr[-half:]

    output = {
        'command': command_str,
        'returncode': result.returncode,
        'stdout': stdout,
        'stderr': stderr,
    }

    if duration is not None:
        output['duration_seconds'] = round(duration, 2)

    return output


def run_tool_with_output(cmd: list[str], timeout: int = None, cwd: str = None) -> tuple[subprocess.CompletedProcess, dict]:
    """
    Run a tool command and return both the result and structured output info.

    This is a convenience wrapper that combines run_tool() with get_process_output()
    and also tracks execution time.

    Args:
        cmd: Command and arguments to run
        timeout: Maximum execution time in seconds
        cwd: Working directory for the command (optional)

    Returns:
        Tuple of (CompletedProcess, process_output_dict)
    """
    start_time = time.time()
    result = run_tool(cmd, timeout, cwd=cwd)
    duration = time.time() - start_time

    output = get_process_output(result, cmd, duration)
    return result, output


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

# vim: ts=4 sw=4 et
