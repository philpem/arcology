"""
Copy-protection scheme registry.

Each ``ProtectionScheme`` describes a removable disc-protection scheme
(probe + applicable filesystems + the AnalysisType that strips it).
Scheme modules register themselves at import time by appending to
``PROTECTION_SCHEMES``; the partition-detection handler calls
:func:`queue_extraction_or_protection_remove` which iterates the
registry.

To add a new scheme:
  1. Create ``analyses/<scheme>.py`` with a ``process_<scheme>_remove``
     handler (analogous to ``armlock.process_armlock_remove``).
  2. Define a ``ProtectionScheme`` describing the probe and append it
     to ``PROTECTION_SCHEMES``.
  3. Import the new module from ``analyses/__init__.py`` (so the
     append runs) and bind the handler onto AnalysisWorker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared.enums import AnalysisType


@dataclass(frozen=True)
class ProtectionScheme:
    """Description of a removable copy-protection scheme."""

    name: str
    applicable_filesystems: frozenset[str]
    analysis_type: AnalysisType
    detect: Callable[[Path], bool]


PROTECTION_SCHEMES: list[ProtectionScheme] = []


def queue_extraction_or_protection_remove(
    worker,
    target_uuid: str,
    image_path: Path,
    fs: str,
    partition_index: int,
    *,
    container_format: str | None = None,
    partition_image_path: str | None = None,
) -> None:
    """Probe ``image_path`` against every registered protection scheme.

    If any scheme matches, queue its removal analysis with the standard
    extraction hints; otherwise queue FILE_EXTRACTION directly.  Probes
    are cheap and pure-Python, so running them inline during partition
    detection avoids the cost of an extra analysis pass.
    """
    for scheme in PROTECTION_SCHEMES:
        if fs not in scheme.applicable_filesystems:
            continue
        if not scheme.detect(image_path):
            continue
        hints: dict = {
            'filesystem': fs,
            'partition_index': partition_index,
        }
        if container_format:
            hints['container_format'] = container_format
        if partition_image_path:
            hints['partition_image_path'] = partition_image_path
        worker.api.queue_analysis(
            target_uuid,
            scheme.analysis_type.value,
            hints=hints,
        )
        return

    worker.queue_file_extraction(
        target_uuid,
        fs,
        partition_index,
        partition_image_path=partition_image_path,
        container_format=container_format,
    )
# vim: ts=4 sw=4 et
