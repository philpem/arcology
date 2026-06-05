"""
Shared definitions for arco disk-image bundles.

A disk-image *bundle* is a ZIP produced by ``arco bulk-import --bundle-sidecars``
containing exactly one (usually compressed) disk image plus small sidecar files
(a ddrescue ``.map``, a readme, checksums).  The worker recognises such a bundle
and stores the image as a single disk-image artefact rather than extracting the
zip as a generic archive.

The CLI bundle builder (``cli/arccli/commands/bulk_import.py``) keeps its OWN
copies of these constants because the ``arco`` package is installed standalone
and cannot import ``shared``.  A CI test (``ci/test_bundle_marker.py``) asserts
the two stay in sync, so this module remains the source of truth for the worker
and web app while the CLI copy is drift-checked.
"""

from pathlib import Path

# Written by the CLI into the ZIP archive comment; read by the worker to
# recognise a bundle.  Pure ASCII so it round-trips through cp437 unchanged.
BUNDLE_MARKER = 'arcology:disk-image-bundle/v1'

# Loose files bundled with a disk image even when they do not share its base
# name (a generic readme, a CHECKSUMS file, etc.).
SIDECAR_NAME_PREFIXES = ('readme', 'read.me', 'changelog', 'changes',
                         'checksum', 'md5', 'sha1', 'sha256', 'sha512')
SIDECAR_EXTENSIONS = ('.md5', '.sha1', '.sha256', '.sha512')


def is_sidecar_name(filename: str, image_base: str) -> bool:
    """Whether *filename* is a sidecar for an image whose base name is *image_base*.

    *image_base* is the image filename with its importable extension stripped,
    lowercased (e.g. ``'drive'`` for ``'drive.dd.zst'``).  A file qualifies if it
    shares that base name (``drive.map``, ``drive.txt``), carries a checksum
    extension, or starts with a known sidecar name prefix.
    """
    name = filename.lower()
    if image_base and name.startswith(image_base + '.'):
        return True
    if Path(name).suffix in SIDECAR_EXTENSIONS:
        return True
    return any(name.startswith(p) for p in SIDECAR_NAME_PREFIXES)

# vim: ts=4 sw=4 et
