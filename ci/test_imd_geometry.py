"""
Unit tests for IMD parser and filesystem geometry detection.

Tests parse_imd_track0() and detect_geometry_from_boot_data() using
synthetic IMD files constructed entirely in memory — no real disc images
or external tools required.

Run:
    python -m unittest ci.test_imd_geometry -v
"""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.tools.imd import parse_imd_track0, detect_geometry_from_boot_data
from worker.arcworker.tools.flux import _geometry_to_gw_format


# =============================================================================
# IMD builder helpers
# =============================================================================

def _make_imd(tracks: list[dict]) -> bytes:
    """
    Build a minimal synthetic IMD file from a list of track dicts.

    Each track dict must have:
        mode      int   IMD mode byte (0-2=FM, 3-5=MFM)
        cylinder  int
        head      int   (0 or 1; no flags set in head byte)
        sectors   dict  {sector_id: bytes}  — all sectors must be same size
    """
    out = b'IMD test\x1A'
    for t in tracks:
        smap = t['sectors']
        ids  = sorted(smap.keys())
        nsec = len(ids)
        if ids:
            sec_bytes = next(iter(smap.values()))
            size_code = {128: 0, 256: 1, 512: 2, 1024: 3, 2048: 4}[len(sec_bytes)]
        else:
            size_code = 2

        out += bytes([t['mode'], t['cylinder'], t['head'], nsec, size_code])
        out += bytes(ids)
        for sid in ids:
            out += b'\x01'          # raw sector type
            out += smap[sid]
    return out


def _write_imd(tracks: list[dict]) -> Path:
    """Write a synthetic IMD to a temp file and return its Path."""
    data  = _make_imd(tracks)
    fd, path = tempfile.mkstemp(suffix='.imd')
    os.write(fd, data)
    os.close(fd)
    return Path(path)


def _make_fat_bpb(
    spt: int = 9,
    heads: int = 2,
    cylinders: int = 80,
    bps: int = 512,
) -> bytes:
    """Return a minimal 512-byte FAT12/16 boot sector that passes detect_fat_filesystem."""
    total = cylinders * heads * spt
    buf   = bytearray(512)
    # BPB_BytsPerSec
    struct.pack_into('<H', buf, 11, bps)
    # BPB_SecPerClus
    buf[13] = 1
    # BPB_RsvdSecCnt
    struct.pack_into('<H', buf, 14, 1)
    # BPB_NumFATs
    buf[16] = 2
    # BPB_RootEntCnt
    struct.pack_into('<H', buf, 17, 224)
    # BPB_TotSec16
    struct.pack_into('<H', buf, 19, total)
    # BPB_Media
    buf[21] = 0xF9
    # BPB_FATSz16
    struct.pack_into('<H', buf, 22, 3)
    # BPB_SecPerTrk
    struct.pack_into('<H', buf, 24, spt)
    # BPB_NumHeads
    struct.pack_into('<H', buf, 26, heads)
    # Boot sector signature
    buf[510] = 0x55
    buf[511] = 0xAA
    return bytes(buf)


def _make_adfs_floppy_sector0(
    log2ss: int = 10,
    spt: int = 5,
    heads: int = 2,
    disc_size: int = 800 * 1024,
    directory_magic: bytes = b'Hugo',
) -> bytes:
    """
    Return a 1024-byte sector 0 for an ADFS floppy new-map disc.

    The first 512 bytes form the Filecore floppy boot block (mod-256 sum = 0).
    The disc record begins at byte 4.  Bytes 0x200-0x204 hold optional
    directory magic (Hugo/SBPr) which is ignored by the probes but kept for
    test completeness.
    """
    buf = bytearray(1024)
    # Disc record at byte 4
    buf[4] = log2ss
    buf[5] = spt
    buf[6] = heads
    struct.pack_into('<I', buf, 0x14, disc_size)

    # Directory magic for D vs E detection (within 1024B sector)
    if directory_magic == b'Hugo':
        buf[0x200] = 0x00          # master sequence number placeholder
        buf[0x201:0x205] = b'Hugo'
    elif directory_magic == b'SBPr':
        buf[0x200:0x204] = b'SBPr'

    # Adjust byte 3 so sum(buf[0:512]) % 256 == 0
    total      = sum(buf[0:512]) & 0xFF
    buf[3]     = (-total) & 0xFF
    return bytes(buf)


# =============================================================================
# parse_imd_track0 — geometry parsing
# =============================================================================

class TestParseImdTrack0(unittest.TestCase):

    def _parse(self, tracks: list[dict]) -> dict:
        path = _write_imd(tracks)
        try:
            return parse_imd_track0(path)
        finally:
            path.unlink(missing_ok=True)

    def test_mfm_80_2_16_256_adfs_l(self):
        # MFM, 80 cylinders, 2 heads, 16 sectors/track, 256B — ADFS-L geometry
        tracks = []
        for cyl in range(80):
            for head in range(2):
                sids = {i: bytes(256) for i in range(16)}
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head, 'sectors': sids})
        result = self._parse(tracks)
        self.assertIsNotNone(result)
        self.assertEqual(result['encoding'],    'MFM')
        self.assertEqual(result['sector_size'], 256)
        self.assertEqual(result['cylinders'],   80)
        self.assertEqual(result['heads'],       2)
        self.assertEqual(len(result['sectors']), 16)

    def test_mfm_80_2_5_1024_adfs(self):
        # MFM, 80 cylinders, 2 heads, 5 sectors/track, 1024B — ADFS-D geometry
        tracks = []
        for cyl in range(80):
            for head in range(2):
                sids = {i: bytes(1024) for i in range(5)}
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head, 'sectors': sids})
        result = self._parse(tracks)
        self.assertIsNotNone(result)
        self.assertEqual(result['encoding'],    'MFM')
        self.assertEqual(result['sector_size'], 1024)
        self.assertEqual(result['cylinders'],   80)
        self.assertEqual(result['heads'],       2)
        self.assertEqual(len(result['sectors']), 5)

    def test_fm_40_1_10_256_dfs(self):
        # FM, 40 cylinders, 1 head, 10 sectors/track, 256B — DFS geometry
        tracks = []
        for cyl in range(40):
            sids = {i: bytes(256) for i in range(10)}
            tracks.append({'mode': 0, 'cylinder': cyl, 'head': 0, 'sectors': sids})
        result = self._parse(tracks)
        self.assertIsNotNone(result)
        self.assertEqual(result['encoding'],    'FM')
        self.assertEqual(result['sector_size'], 256)
        self.assertEqual(result['cylinders'],   40)
        self.assertEqual(result['heads'],       1)

    def test_invalid_file_returns_none(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b'This is not an IMD file')
        os.close(fd)
        try:
            self.assertIsNone(parse_imd_track0(Path(path)))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(parse_imd_track0(Path('/tmp/nonexistent_imd_12345.imd')))

    def test_sector_data_stored_for_track0_only(self):
        # Sector data for track 1 must NOT appear in the returned sectors dict
        tracks = [
            {'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': {1: b'\xAA' * 512}},
            {'mode': 3, 'cylinder': 1, 'head': 0, 'sectors': {1: b'\xBB' * 512}},
        ]
        result = self._parse(tracks)
        self.assertIsNotNone(result)
        self.assertIn(1, result['sectors'])
        self.assertEqual(result['sectors'][1], b'\xAA' * 512)

    def test_duplicate_sector_id_first_wins(self):
        # When two sectors on track 0 share the same ID (copy-protection trick),
        # the FIRST occurrence must be stored — subsequent ones must not overwrite.
        real_data = b'\xAA' * 512
        fake_data = b'\xBB' * 512
        header  = b'IMD test\x1A'
        # Two sectors both claiming ID=1: real first, fake second
        track_hdr = bytes([3, 0, 0, 2, 2])      # mode=3, cyl=0, head=0, nsec=2, size=512B
        sector_id_map = bytes([1, 1])            # both claim ID 1
        sector_rec1 = b'\x01' + real_data        # type=1 (raw), real data
        sector_rec2 = b'\x01' + fake_data        # type=1 (raw), fake (protection) data
        imd = header + track_hdr + sector_id_map + sector_rec1 + sector_rec2

        fd, path = tempfile.mkstemp(suffix='.imd')
        os.write(fd, imd)
        os.close(fd)
        try:
            result = parse_imd_track0(Path(path))
            self.assertIsNotNone(result)
            self.assertIn(1, result['sectors'])
            self.assertEqual(result['sectors'][1], real_data,
                             "First sector with duplicate ID should be kept, not overwritten")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_compressed_sector_expanded(self):
        # Build IMD manually with a compressed sector (type 2, fill byte 0xCC)
        header  = b'IMD test\x1A'
        # mode=3, cyl=0, head=0, nsec=1, size_code=2 (512B)
        track_hdr = bytes([3, 0, 0, 1, 2])
        sector_id_map = bytes([5])
        sector_data   = bytes([2, 0xCC])   # type=2 (compressed), fill=0xCC
        imd = header + track_hdr + sector_id_map + sector_data

        fd, path = tempfile.mkstemp(suffix='.imd')
        os.write(fd, imd)
        os.close(fd)
        try:
            result = parse_imd_track0(Path(path))
            self.assertIsNotNone(result)
            self.assertIn(5, result['sectors'])
            self.assertEqual(result['sectors'][5], bytes([0xCC] * 512))
        finally:
            Path(path).unlink(missing_ok=True)


# =============================================================================
# detect_geometry_from_boot_data — filesystem probes
# =============================================================================

class TestDetectGeometry(unittest.TestCase):

    def _detect(self, tracks: list[dict]) -> dict | None:
        path = _write_imd(tracks)
        try:
            track0 = parse_imd_track0(path)
            if track0 is None:
                return None
            return detect_geometry_from_boot_data(track0)
        finally:
            path.unlink(missing_ok=True)

    # ── Probe A: DFS (FM) ──────────────────────────────────────────────────

    def test_probe_a_dfs_fm_ss80(self):
        tracks = []
        for cyl in range(80):
            sids = {i: bytes(256) for i in range(10)}
            tracks.append({'mode': 0, 'cylinder': cyl, 'head': 0, 'sectors': sids})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],       'dfs')
        self.assertEqual(geo['sectors_per_track'], 10)
        self.assertEqual(geo['sector_size'],       256)
        self.assertEqual(geo['encoding'],          'FM')

    def test_probe_a_dfs_fm_ds(self):
        tracks = []
        for cyl in range(40):
            for head in range(2):
                sids = {i: bytes(256) for i in range(10)}
                tracks.append({'mode': 0, 'cylinder': cyl, 'head': head, 'sectors': sids})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'], 'dfs')
        self.assertEqual(geo['heads'],      2)

    # ── Probe D: ADFS floppy new-map ──────────────────────────────────────

    def test_probe_d_adfs_800k_floppy(self):
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=5, heads=2,
            disc_size=800*1024, directory_magic=b'Hugo'
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(5)}}]
        for cyl in range(1, 80):
            tracks.append({'mode': 3, 'cylinder': cyl, 'head': 0,
                           'sectors': {i: bytes(1024) for i in range(5)}})
            tracks.append({'mode': 3, 'cylinder': cyl, 'head': 1,
                           'sectors': {i: bytes(1024) for i in range(5)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],        'adfs')
        self.assertEqual(geo['sectors_per_track'],  5)
        self.assertEqual(geo['sector_size'],        1024)
        self.assertEqual(geo['heads'],              2)

    def test_probe_d_adfs_800k_sbpr_gives_adfs(self):
        # SBPr is no longer examined; geometry alone selects the format.
        # A disc with SBPr in the boot sector still yields filesystem='adfs'.
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=5, heads=2,
            disc_size=800*1024, directory_magic=b'SBPr'
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(5)}}]
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'], 'adfs')
        self.assertEqual(geo['sectors_per_track'], 5)

    def test_probe_d_adfs_1600k(self):
        # ADFS-F: 80 cyl × 2 heads × 10 SPT × 1024B → 1600K
        # Disc record says spt=10 → must be identified as adfs (not fall back)
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=10, heads=2,
            disc_size=1600*1024, directory_magic=b'Hugo'  # Hugo present but irrelevant for F
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(10)}}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(10)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],        'adfs')
        self.assertEqual(geo['sectors_per_track'],  10)
        self.assertEqual(geo['sector_size'],        1024)
        self.assertEqual(geo['heads'],              2)
        self.assertEqual(geo['probe'],              'D')

    def test_probe_d_adfs_1600k_maps_to_acorn_adfs_1600(self):
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=10, heads=2, disc_size=1600*1024
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(10)}}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(10)}})
        path = _write_imd(tracks)
        try:
            track0 = parse_imd_track0(path)
            geo    = detect_geometry_from_boot_data(track0)
            fmt    = _geometry_to_gw_format(
                filesystem=geo['filesystem'], cylinders=geo['cylinders'],
                heads=geo['heads'], sectors_per_track=geo['sectors_per_track'],
                sector_size=geo['sector_size'],
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(fmt, 'acorn.adfs.1600')

    def test_probe_d_adfs_1600k_sbpr_still_gives_adfs(self):
        # No directory magic is checked; a disc with SBPr and spt=10 still gives adfs
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=10, heads=2,
            disc_size=1600*1024, directory_magic=b'SBPr'
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(10)}}]
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'], 'adfs')

    def test_probe_d_cylinders_from_disc_size_not_imd_count(self):
        # Probe D must derive cylinder count from the disc record's disc_size
        # field, NOT from the number of tracks in the IMD.  hxcfe often writes
        # extra empty padding tracks beyond the actual disc capacity.
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=5, heads=2, disc_size=800*1024
        )
        # Build a 3-track IMD (far fewer than 80) — cylinders should still be 80
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(5)}}]
        for cyl in range(1, 3):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(5)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'], 'adfs')
        self.assertEqual(geo['cylinders'],  80)   # from disc_size, not IMD track count

    def test_probe_d_extra_imd_cylinders_maps_to_800k(self):
        # Regression: hxcfe writes 82 cylinders in the IMD for an 80-cylinder
        # ADFS-D disc.  Format detection must still return 'acorn.adfs.800'.
        sec0 = _make_adfs_floppy_sector0(
            log2ss=10, spt=5, heads=2, disc_size=800*1024
        )
        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(5)}}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(5)}})
        # Two extra empty padding cylinders (as hxcfe writes)
        for cyl in range(80, 82):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(5)}})
        path = _write_imd(tracks)
        try:
            track0 = parse_imd_track0(path)
            self.assertEqual(track0['cylinders'], 82)   # raw IMD count
            geo = detect_geometry_from_boot_data(track0)
            self.assertIsNotNone(geo)
            self.assertEqual(geo['cylinders'], 80)       # from disc record
            fmt = _geometry_to_gw_format(
                filesystem=geo['filesystem'], cylinders=geo['cylinders'],
                heads=geo['heads'], sectors_per_track=geo['sectors_per_track'],
                sector_size=geo['sector_size'],
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(fmt, 'acorn.adfs.800')

    def test_probe_d_no_checksum_detects_adfs_via_disc_size_alignment(self):
        # Regression: real ADFS-D discs converted via flux→HFE→IMD by hxcfe
        # sometimes produce sector 0 data where neither the 512B nor the 1024B
        # mod-256 sum equals zero.  Probe D must still fire via the disc_size
        # field alignment fallback (disc_size % sector_size == 0).
        buf = bytearray(1024)
        buf[4] = 10                                 # log2ss = 10 → 1024B sectors
        buf[5] = 5                                  # spt
        buf[6] = 2                                  # heads
        struct.pack_into('<I', buf, 0x14, 800 * 1024)  # disc_size = 800K
        # buf[3] stays 0 → sum(buf[0:512]) == 157 ≠ 0 → both checksums fail
        sec0 = bytes(buf)
        # Guard: confirm neither checksum variant passes
        self.assertNotEqual(sum(sec0[0:512]) & 0xFF, 0)
        self.assertNotEqual(sum(sec0) & 0xFF, 0)

        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0,
                   'sectors': {i: (sec0 if i == 0 else bytes(1024)) for i in range(5)}}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(1024) for i in range(5)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],        'adfs')
        self.assertEqual(geo['sectors_per_track'],  5)
        self.assertEqual(geo['cylinders'],          80)   # from disc_size
        self.assertEqual(geo['probe'],              'D')

    # ── Probe B: Old-map ADFS (Hugo) ──────────────────────────────────────

    def test_probe_b_adfs_old_hugo(self):
        # Track 0 with 16 sectors/256B; sector ID 2 carries Hugo magic at bytes 1-4
        sectors = {}
        for i in range(16):
            if i == 2:
                buf    = bytearray(256)
                buf[0] = 0x01          # master sequence number
                buf[1:5] = b'Hugo'
                sectors[i] = bytes(buf)
            else:
                sectors[i] = bytes(256)

        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(256) for i in range(16)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],        'adfs')
        self.assertEqual(geo['sectors_per_track'],  16)
        self.assertEqual(geo['sector_size'],        256)

    def test_probe_b_adfs_old_maps_to_format(self):
        # End-to-end: ADFS-L (80/2/16/256) should map to acorn.adfs.640
        sectors = {}
        for i in range(16):
            buf    = bytearray(256)
            if i == 2:
                buf[1:5] = b'Hugo'
            sectors[i] = bytes(buf)

        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(256) for i in range(16)}})
        path = _write_imd(tracks)
        try:
            track0  = parse_imd_track0(path)
            geo     = detect_geometry_from_boot_data(track0)
            fmt     = _geometry_to_gw_format(**geo)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(fmt, 'acorn.adfs.640')

    # ── Probe C: FAT BPB ──────────────────────────────────────────────────

    def test_probe_c_fat_720k(self):
        bpb     = _make_fat_bpb(spt=9, heads=2, cylinders=80, bps=512)
        # IBM convention: sector IDs 1-9 on track 0
        sectors = {}
        for i in range(1, 10):
            sectors[i] = bpb if i == 1 else bytes(512)

        tracks = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(512) for i in range(1, 10)}})
        geo = self._detect(tracks)
        self.assertIsNotNone(geo)
        self.assertEqual(geo['filesystem'],        'fat')
        self.assertEqual(geo['sectors_per_track'],  9)
        self.assertEqual(geo['heads'],              2)
        self.assertEqual(geo['sector_size'],        512)

    def test_probe_c_fat_maps_to_ibm_720(self):
        bpb     = _make_fat_bpb(spt=9, heads=2, cylinders=80, bps=512)
        sectors = {i: (bpb if i == 1 else bytes(512)) for i in range(1, 10)}
        tracks  = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(512) for i in range(1, 10)}})
        path = _write_imd(tracks)
        try:
            track0  = parse_imd_track0(path)
            geo     = detect_geometry_from_boot_data(track0)
            fmt     = _geometry_to_gw_format(**geo)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(fmt, 'ibm.720')

    def test_probe_c_fat_maps_to_ibm_1440(self):
        bpb     = _make_fat_bpb(spt=18, heads=2, cylinders=80, bps=512)
        sectors = {i: (bpb if i == 1 else bytes(512)) for i in range(1, 19)}
        tracks  = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        for cyl in range(1, 80):
            for head in range(2):
                tracks.append({'mode': 3, 'cylinder': cyl, 'head': head,
                               'sectors': {i: bytes(512) for i in range(1, 19)}})
        path = _write_imd(tracks)
        try:
            track0  = parse_imd_track0(path)
            geo     = detect_geometry_from_boot_data(track0)
            fmt     = _geometry_to_gw_format(**geo)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(fmt, 'ibm.1440')

    # ── Unknown geometry ───────────────────────────────────────────────────

    def test_unknown_geometry_returns_none(self):
        # MFM 512B sectors with no recognizable signatures → None
        sectors = {i: bytes(512) for i in range(1, 10)}
        tracks  = [{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': sectors}]
        geo = self._detect(tracks)
        self.assertIsNone(geo)

    def test_geometry_to_gw_format_unknown_returns_none(self):
        self.assertIsNone(
            _geometry_to_gw_format('fat', 99, 99, 99, 512)
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
