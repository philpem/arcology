"""
Base utilities for running external tools.
"""

import hashlib
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from ..config import TOOL_TIMEOUT, log
from ..exceptions import JobCancelledException
from ..utils.text import sanitize_filename

# Module-level cancellation event.  Set by the monitoring thread in AnalysisWorker
# when it detects that the current job has been deleted or is no longer running.
# Cleared at the start of each job so stale signals don't carry over.
# Safe as module-level state because each worker *process* handles one job at a time.
_cancel_event = threading.Event()


def set_cancel_event() -> None:
    """Signal that the current job should be aborted."""
    _cancel_event.set()


def clear_cancel_event() -> None:
    """Reset the cancel signal before starting a new job."""
    _cancel_event.clear()


def is_cancelled() -> bool:
    """True if cancellation has been requested for the current job."""
    return _cancel_event.is_set()


def run_tool(cmd: list[str], timeout: int | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """
    Run a tool command with logging.

    Args:
        cmd: Command and arguments to run
        timeout: Maximum execution time in seconds
        cwd: Working directory for the command (optional)

    Returns:
        CompletedProcess with stdout, stderr, and returncode

    Raises:
        JobCancelledException: If the job was cancelled while the tool was running.
        subprocess.TimeoutExpired: If the tool exceeded the timeout.
    """
    if timeout is None:
        timeout = TOOL_TIMEOUT
    log.debug(f"Running: {' '.join(cmd)}" + (f" (cwd={cwd})" if cwd else ""))

    # Check before spawning the process so we don't start work we'll immediately cancel.
    if _cancel_event.is_set():
        raise JobCancelledException(f"Job cancelled before subprocess started: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    deadline = time.monotonic() + timeout
    stdout = b''
    stderr = b''
    try:
        while True:
            if _cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise JobCancelledException(
                    f"Job cancelled while running: {' '.join(cmd)}"
                )
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
                break  # process finished normally
            except subprocess.TimeoutExpired:
                # This 1-second tick timeout is an implementation detail; only
                # surface a TimeoutExpired once the real wall-clock deadline passes.
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
                # Still within wall-clock limit — loop and re-check cancel.
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        raise

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
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


def run_tool_with_output(cmd: list[str], timeout: int | None = None, cwd: str | None = None) -> tuple[subprocess.CompletedProcess, dict]:
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


def tool_result(
    success: bool,
    *,
    tool: str,
    error: str | None = None,
    process_output: dict | str | None = None,
    **extra,
) -> dict:
    """
    Build a standard tool-wrapper result dict.

    Every tool wrapper in the worker returns a dict with at least ``success``
    and ``tool`` keys. Optional fields (``error`` on failure, ``process_output``
    from :func:`run_tool_with_output`, and any tool-specific extras such as
    ``output_path``, ``output_type``, ``summary``, ``file_count``) are included
    when provided.

    Args:
        success: True on success, False on failure.
        tool: Name of the tool that produced this result.
        error: Error message (only relevant when success=False).
        process_output: Structured subprocess output dict from
            :func:`get_process_output`, or a plain string.
        **extra: Any additional caller-specific keys.
    """
    result: dict = {'success': success, 'tool': tool}
    if error is not None:
        result['error'] = error
    if process_output is not None:
        result['process_output'] = process_output
    result.update(extra)
    return result


def run_and_build_result(
    cmd: list[str],
    *,
    tool: str,
    output_path: Path,
    summary: str,
    timeout: int | None = None,
    cwd: str | None = None,
    stderr_truncate: int = 1000,
    **extras,
) -> dict:
    """
    Run a subprocess and build a standard success/failure result dict.

    Captures the "returncode == 0 AND output file exists" idiom used across
    flux.py, extraction.py and partition.py. On success, returns a result dict
    with ``output_path``, ``summary``, ``process_output`` and any additional
    ``**extras`` (e.g. ``output_type``, ``gw_format``). On failure, returns a
    result dict with ``error`` (truncated stderr) and the same extras.

    Args:
        cmd: Command list to execute.
        tool: Tool name for the result dict.
        output_path: Required output file; its existence is part of the
            success check.
        summary: Human-readable summary (included on success only).
        timeout: Subprocess timeout in seconds.
        cwd: Working directory.
        stderr_truncate: Characters of stderr to include in the error message.
        **extras: Additional keys included in both success and failure result
            dicts (e.g. ``output_type``, ``gw_format``, ``heads``).
    """
    result, process_output = run_tool_with_output(cmd, timeout=timeout, cwd=cwd)

    if result.returncode == 0 and output_path.exists():
        return tool_result(
            True,
            tool=tool,
            output_path=str(output_path),
            summary=summary,
            process_output=process_output,
            **extras,
        )

    return tool_result(
        False,
        tool=tool,
        error=result.stderr.decode(errors='replace')[:stderr_truncate],
        process_output=process_output,
        **extras,
    )


def exception_result(
    tool: str,
    error_prefix: str,
    *,
    process_output: dict | None = None,
    trace_truncate: int = 2000,
    **extra,
) -> dict:
    """
    Build a failure result dict from inside an ``except`` clause.

    Captures the current exception and its traceback, producing the same
    shape as :func:`tool_result` plus an ``exception_trace`` key. Use this
    for the catch-all ``except Exception`` blocks in tool wrappers where
    the error message is "{prefix}: {exception}" and the traceback should
    be preserved for later diagnosis.

    Args:
        tool: Tool name for the result dict.
        error_prefix: Human-readable prefix for the error message.
        process_output: Subprocess output captured before the exception
            (may be ``None`` if the exception fired before the subprocess ran).
        trace_truncate: Maximum characters of traceback to retain.
        **extra: Additional keys to include in the result dict.
    """
    exc = sys.exc_info()[1]
    return tool_result(
        False,
        tool=tool,
        error=f'{error_prefix}: {exc}',
        process_output=process_output,
        exception_trace=traceback.format_exc()[:trace_truncate],
        **extra,
    )


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
