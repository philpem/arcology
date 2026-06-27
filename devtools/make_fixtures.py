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
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / 'ci' / 'integration' / 'fixtures'

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
# Fixed mtime embedded in tool-built filesystem images (mcopy stamps FAT
# directory entries from the source file's mtime) so rebuilds are byte-stable.
_FS_EPOCH = 631152000  # 1990-01-01 00:00:00 UTC


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


def _require(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None
               and not Path(f'/usr/sbin/{t}').exists()]
    if missing:
        raise SystemExit(
            f"missing tools to build this fixture: {', '.join(missing)}. "
            f"The committed binary is authoritative; only rebuild where these "
            f"tools are available (e.g. the worker container)."
        )


def make_fat_720k() -> None:
    """Build a 720K whole-disc FAT12 image (no partition table).

    Tool-built (mkfs.vfat + mcopy) and committed as a binary; this builder
    documents its provenance and is byte-reproducible thanks to a fixed volume
    id (``-i``) and fixed source-file mtimes (mcopy stamps directory entries
    from them).  PARTITION_DETECT identifies it via pure-Python BPB parsing;
    sfdisk reports no table and the file(1) clause is normalised away.
    """
    _require('mkfs.vfat', 'mcopy')
    case_dir = FIXTURES / 'fat_720k'
    case_dir.mkdir(parents=True, exist_ok=True)
    image = case_dir / 'disk.img'

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        img = tmp / 'disk.img'
        with open(img, 'wb') as fh:
            fh.write(b'\x00' * (720 * 1024))
        subprocess.run(
            ['mkfs.vfat', '-F', '12', '-i', 'DEADBEEF', '-n', 'ARCOLOGY', str(img)],
            check=True, capture_output=True,
        )
        # Deterministic content with fixed mtimes so mcopy's stamps are stable.
        members = {
            'README.TXT': b'arcology fat12 fixture\n',
            'DATA.BIN': bytes(range(64)) * 2,
        }
        env = {**os.environ, 'MTOOLS_SKIP_CHECK': '1'}
        for name, data in members.items():
            src = tmp / name
            src.write_bytes(data)
            os.utime(src, (_FS_EPOCH, _FS_EPOCH))
            subprocess.run(
                ['mcopy', '-i', str(img), str(src), f'::/{name}'],
                check=True, capture_output=True, env=env,
            )
        shutil.copy(img, image)

    _write_manifest('fat_720k', {
        'input': 'disk.img',
        'original_filename': 'disk.img',
        'artefact_type': 'RAW_SECTOR',
        'seed_analyses': [{'type': 'partition_detect'}],
        # Detect only; FILE_EXTRACTION is queued by the handler and asserted in
        # final_queue (running it would need the in-container 7z/mtools path).
        'run_types': ['partition_detect'],
        'required_tools': ['sfdisk', 'file'],
        'max_steps': 20,
    })


def make_mbr_2part() -> None:
    """Build a 4MB image with a 2-partition MBR table (no real filesystems).

    sfdisk reads the partition *table*, not partition contents, so the
    partitions are left zero-filled — PARTITION_DETECT carves each as a derived
    RAW_SECTOR artefact (FILE_EXTRACTION queued per partition, asserted in
    final_queue) using the filesystem implied by the MBR type byte.  The
    inter/post-partition gaps are uniform zero fill and are omitted with a note;
    the pre-partition region (containing the MBR) is non-uniform and is
    registered as an UNKNOWN gap artefact.

    Byte-reproducible: a fixed ``label-id`` pins the MBR disk signature that
    sfdisk would otherwise randomise.
    """
    _require('sfdisk')
    case_dir = FIXTURES / 'mbr_2part'
    case_dir.mkdir(parents=True, exist_ok=True)
    image = case_dir / 'disk.img'

    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / 'disk.img'
        with open(img, 'wb') as fh:
            fh.write(b'\x00' * (8192 * 512))  # 4 MB
        script = (
            'label: dos\n'
            'label-id: 0x12345678\n'
            'unit: sectors\n'
            'start=2048, size=2048, type=06\n'   # FAT16
            'start=6144, size=1024, type=0b\n'   # FAT32
        )
        sfdisk = shutil.which('sfdisk') or '/usr/sbin/sfdisk'
        subprocess.run([sfdisk, str(img)], input=script.encode(),
                       check=True, capture_output=True)
        shutil.copy(img, image)

    _write_manifest('mbr_2part', {
        'input': 'disk.img',
        'original_filename': 'disk.img',
        'artefact_type': 'RAW_SECTOR',
        'seed_analyses': [{'type': 'partition_detect'}],
        # Detect only; the per-partition FILE_EXTRACTION jobs are asserted in
        # final_queue rather than executed.
        'run_types': ['partition_detect'],
        'required_tools': ['sfdisk', 'file'],
        'max_steps': 20,
    })


_FIXTURES = {
    'zip_plain': make_zip_plain,
    'tar_gz': make_tar_gz,
    'zip_promote': make_zip_promote,
    'fat_720k': make_fat_720k,
    'mbr_2part': make_mbr_2part,
}

# Pure-Python fixtures buildable anywhere; `all` builds only these.  Tool-built
# fixtures (FAT, MBR, …) must be named explicitly and need external tools.
_PURE_PYTHON = ('zip_plain', 'tar_gz', 'zip_promote')


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        print('cases:', ', '.join(sorted(_FIXTURES)))
        print('all   :', ', '.join(_PURE_PYTHON), '(pure-Python only)')
        return 0
    target = argv[0]
    cases = sorted(_PURE_PYTHON) if target == 'all' else [target]
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
