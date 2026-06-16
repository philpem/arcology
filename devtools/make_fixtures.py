#!/usr/bin/env python3
"""Fabricate the committed integration-test fixtures, deterministically.

Each subcommand writes a fixture binary plus a ``manifest.json`` under
``ci/integration/fixtures/<case>/``.  All inputs are built with fixed
timestamps so re-running this produces byte-identical files (stable git diffs,
stable extracted-file mtimes once normalisation drops them anyway).

Usage:
    python3 devtools/make_fixtures.py all
    python3 devtools/make_fixtures.py zip_plain
"""

import gzip
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / 'ci' / 'integration' / 'fixtures'

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _write_manifest(case: str, manifest: dict) -> None:
    case_dir = FIXTURES / case
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / 'manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n'
    )


def _deterministic_zip(members: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP with fixed timestamps and stored ordering."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _deterministic_tar_gz(members: list[tuple[str, bytes]]) -> bytes:
    """Build a gzip-compressed tar with fixed mtimes and gzip mtime=0."""
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode='w') as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tf.addfile(info, io.BytesIO(data))
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode='wb', mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    return gz_buf.getvalue()


# ── fixtures ────────────────────────────────────────────────────────────
def make_zip_plain() -> None:
    inner = _deterministic_zip([('inner/secret.txt', b'nested payload\n')])
    archive = _deterministic_zip([
        ('readme.txt', b'hello arcology\n'),
        ('data/numbers.bin', bytes(range(32))),
        ('inner.zip', inner),
    ])
    (FIXTURES / 'zip_plain').mkdir(parents=True, exist_ok=True)
    (FIXTURES / 'zip_plain' / 'archive.zip').write_bytes(archive)
    _write_manifest('zip_plain', {
        'input': 'archive.zip',
        'original_filename': 'archive.zip',
        'artefact_type': 'ZIP',
        'seed_analyses': [{'type': 'archive_extract'}],
        # Run both extract and detect so the nested inner.zip is detected and
        # recursively extracted (parent_file_id chaining, depth tracking).
        'run_types': ['archive_extract', 'archive_detect'],
        'required_tools': ['unzip'],
        'max_steps': 20,
    })


def make_tar_gz() -> None:
    archive = _deterministic_tar_gz([
        ('docs/notes.txt', b'tar notes\n'),
        ('bin/blob.dat', bytes([0, 1, 2, 3, 4, 5, 6, 7])),
    ])
    (FIXTURES / 'tar_gz').mkdir(parents=True, exist_ok=True)
    (FIXTURES / 'tar_gz' / 'archive.tar.gz').write_bytes(archive)
    _write_manifest('tar_gz', {
        'input': 'archive.tar.gz',
        'original_filename': 'archive.tar.gz',
        'artefact_type': 'TARGZ',
        'seed_analyses': [{'type': 'archive_extract'}],
        'run_types': ['archive_extract'],
        'required_tools': ['gzip', 'tar'],
        'max_steps': 20,
    })


def make_zip_promote() -> None:
    # A small, arbitrary "disc image": content is never decoded in this slice
    # (PARTITION_DETECT is queued but not run), so any bytes suffice.
    adf = b'ARCOLOGY-FAKE-ADF\x00' + bytes(range(64)) * 4
    archive = _deterministic_zip([
        ('disc.adf', adf),
        ('readme.txt', b'promote me\n'),
    ])
    (FIXTURES / 'zip_promote').mkdir(parents=True, exist_ok=True)
    (FIXTURES / 'zip_promote' / 'promote.zip').write_bytes(archive)
    _write_manifest('zip_promote', {
        'input': 'promote.zip',
        'original_filename': 'promote.zip',
        'artefact_type': 'ZIP',
        'seed_analyses': [{'type': 'archive_extract'}],
        'run_types': ['archive_extract'],
        'required_tools': ['unzip'],
        'max_steps': 20,
    })


_FIXTURES = {
    'zip_plain': make_zip_plain,
    'tar_gz': make_tar_gz,
    'zip_promote': make_zip_promote,
}


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        print('cases:', ', '.join(sorted(_FIXTURES)), 'all')
        return 0
    target = argv[0]
    cases = sorted(_FIXTURES) if target == 'all' else [target]
    for case in cases:
        if case not in _FIXTURES:
            print(f"unknown fixture: {case}", file=sys.stderr)
            return 2
        _FIXTURES[case]()
        print(f"wrote fixture: {case}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

# vim: ts=4 sw=4 et
