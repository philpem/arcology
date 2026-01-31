"""
Compression handling utilities.

Handles decompression of compressed input files before analysis.
"""

import shutil
import subprocess
from pathlib import Path

from .config import log


COMPRESSION_EXTENSIONS = {
    '.zst': ['zstd', '-d', '-k', '-f'],
    '.gz': ['gzip', '-d', '-k', '-f'],
    '.bz2': ['bzip2', '-d', '-k', '-f'],
}


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
        RuntimeError: If decompression fails
    """
    suffix = input_path.suffix.lower()

    if suffix in COMPRESSION_EXTENSIONS:
        cmd = COMPRESSION_EXTENSIONS[suffix]
        decompressed_name = input_path.stem  # Remove compression extension
        decompressed_path = work_dir / decompressed_name

        # Copy compressed file to work dir first
        compressed_copy = work_dir / input_path.name
        shutil.copy(input_path, compressed_copy)

        # Decompress
        log.info(f"Decompressing {input_path.name} with {cmd[0]}")
        result = subprocess.run(
            cmd + [str(compressed_copy)],
            capture_output=True,
            cwd=work_dir
        )

        if result.returncode != 0:
            raise RuntimeError(f"Decompression failed: {result.stderr.decode()}")

        # Clean up compressed copy
        compressed_copy.unlink(missing_ok=True)

        return decompressed_path

    return input_path
