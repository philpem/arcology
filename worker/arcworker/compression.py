"""
Compression handling utilities.

Handles decompression of compressed input files before analysis.
"""

import select
import shutil
import subprocess
import time
from pathlib import Path

from .config import log, TOOL_TIMEOUT, MAX_DECOMPRESSED_BYTES


COMPRESSION_EXTENSIONS = {
    '.zst': ['zstd', '-d', '-c'],
    '.gz':  ['gzip', '-d', '-c'],
    '.bz2': ['bzip2', '-d', '-c'],
}

_NOT_COMPRESSED_MARKERS = [
    'not in gzip format',        # gzip
    'is not a bzip2 file',       # bzip2
    'File format not recognized', # zstd (unrecognised magic)
]

_CHUNK = 65536


def stream_to_file(
    proc: subprocess.Popen,
    output_path: Path,
    max_bytes: int,
    timeout: float,
) -> str:
    """Stream proc.stdout to output_path, enforcing byte and time limits.

    Uses select() to poll for data so the read loop never blocks longer than
    ~1 second at a time.  This means the TOOL_TIMEOUT deadline is checked on
    every iteration even if the subprocess produces no output.

    Returns one of:
        'ok'            — completed normally
        'size_exceeded' — killed because output would exceed max_bytes
        'timeout'       — killed because wall-clock time exceeded timeout

    The caller is responsible for calling proc.stdout.close() and proc.wait()
    after this function returns.
    """
    deadline = time.monotonic() + timeout
    written = 0
    with open(output_path, 'wb') as dst:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                return 'timeout'
            # Poll with a 1 s interval so we always re-check the deadline.
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
            if not ready:
                if proc.poll() is not None:
                    break   # process already exited, no more data
                continue    # still running, re-check deadline
            chunk = proc.stdout.read(_CHUNK)
            if not chunk:
                break       # EOF
            written += len(chunk)
            if written > max_bytes:
                proc.kill()
                return 'size_exceeded'
            dst.write(chunk)
    return 'ok'


def decompress_if_needed(input_path: Path, work_dir: Path) -> Path:
    """
    If file is compressed, decompress to work_dir and return new path.
    Otherwise return original path.

    Args:
        input_path: Path to the potentially compressed file
        work_dir: Working directory to decompress into

    Returns:
        Path to the decompressed file (or original if not compressed)

    Raises:
        RuntimeError: If decompression fails, times out, or exceeds size limit
    """
    suffix = input_path.suffix.lower()

    if suffix in COMPRESSION_EXTENSIONS:
        cmd = COMPRESSION_EXTENSIONS[suffix]
        decompressed_name = input_path.stem  # Remove compression extension
        decompressed_path = work_dir / decompressed_name

        compressed_size = input_path.stat().st_size
        log.info(f"Compressed file detected: {input_path.name} ({suffix}, {compressed_size:,} bytes)")

        # Copy compressed file to work dir so the tool runs with a local path.
        compressed_copy = work_dir / input_path.name
        shutil.copy(input_path, compressed_copy)

        # Decompress via stdout, enforcing MAX_DECOMPRESSED_BYTES and TOOL_TIMEOUT
        # mid-stream.  stream_to_file() uses select() so it never blocks longer
        # than ~1 s per iteration regardless of subprocess behaviour.
        log.info(f"Decompressing {input_path.name} with {cmd[0]}")
        proc = subprocess.Popen(
            cmd + [str(compressed_copy)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
        )
        outcome = stream_to_file(proc, decompressed_path, MAX_DECOMPRESSED_BYTES, TOOL_TIMEOUT)
        proc.stdout.close()
        proc.wait()

        if outcome == 'timeout':
            compressed_copy.unlink(missing_ok=True)
            decompressed_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Decompression timed out after {TOOL_TIMEOUT} seconds: {input_path.name}"
            )

        if outcome == 'size_exceeded':
            compressed_copy.unlink(missing_ok=True)
            decompressed_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Decompressed size exceeds {MAX_DECOMPRESSED_BYTES:,} byte limit "
                f"({input_path.name})"
            )

        stderr_text = proc.stderr.read().decode()
        compressed_copy.unlink(missing_ok=True)

        if proc.returncode != 0:
            # If the file isn't actually in the expected compressed format
            # (e.g. named .tar.gz but not gzip), fall back to the original
            # file rather than failing the entire analysis.
            if any(marker in stderr_text for marker in _NOT_COMPRESSED_MARKERS):
                log.warning(
                    f"File {input_path.name} has {suffix} extension but is not "
                    f"actually compressed — proceeding with original file"
                )
                decompressed_path.unlink(missing_ok=True)
                return input_path
            raise RuntimeError(f"Decompression failed: {stderr_text}")

        # Verify decompressed output exists and is non-empty.
        if not decompressed_path.exists() or decompressed_path.stat().st_size == 0:
            raise RuntimeError(
                f"Decompressed file is missing or empty: {decompressed_path}"
            )

        decompressed_size = decompressed_path.stat().st_size
        log.info(
            f"Decompression successful: {decompressed_path.name} "
            f"({decompressed_size:,} bytes, ratio {decompressed_size / compressed_size:.1f}x)"
        )

        return decompressed_path

    return input_path


def extract_partition_range(
    input_path: Path, output_path: Path,
    start_byte: int, size_bytes: int
) -> None:
    """
    Extract a byte range from a file to produce an individual partition image.

    Args:
        input_path: Source disc image
        output_path: Destination file for the partition
        start_byte: Byte offset of partition start
        size_bytes: Size in bytes to extract
    """
    CHUNK_SIZE = 1024 * 1024  # 1 MB
    with open(input_path, 'rb') as src:
        src.seek(start_byte)
        remaining = size_bytes
        with open(output_path, 'wb') as dst:
            while remaining > 0:
                chunk = src.read(min(remaining, CHUNK_SIZE))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)

    actual_size = output_path.stat().st_size
    log.info(
        f"Extracted partition image: {output_path.name} "
        f"({actual_size:,} bytes from offset {start_byte:#x})"
    )


def is_region_uniform(file_path: Path, start_byte: int, size_bytes: int) -> tuple[bool, int]:
    """
    Check whether a byte range in a file is filled with a single repeated value.

    Used to decide whether unpartitioned disc space is worth preserving:
    regions that are entirely zero (or any other uniform fill) are omitted.

    Args:
        file_path: Path to the disc image
        start_byte: Byte offset of the region to check
        size_bytes: Length of the region in bytes

    Returns:
        (is_uniform, fill_byte) -- fill_byte is the repeated value when
        uniform, or -1 when the region contains mixed data.
    """
    if size_bytes == 0:
        return True, 0

    CHUNK_SIZE = 1024 * 1024  # 1 MB
    with open(file_path, 'rb') as f:
        f.seek(start_byte)
        first = f.read(1)
        if not first:
            return True, 0

        fill_value = first[0]
        reference = bytes([fill_value]) * CHUNK_SIZE
        remaining = size_bytes - 1

        while remaining > 0:
            chunk = f.read(min(remaining, CHUNK_SIZE))
            if not chunk:
                break
            expected = reference if len(chunk) == CHUNK_SIZE else reference[:len(chunk)]
            if chunk != expected:
                return False, -1
            remaining -= len(chunk)

    return True, fill_value

# vim: ts=4 sw=4 et
