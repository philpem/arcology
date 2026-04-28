"""
Unit tests for track density mismatch detection.

Tests parse_imd_tracks() and detect_track_density_mismatch() using synthetic
IMD files built entirely in memory — no real disc images or external tools needed.

Run:
    python -m unittest ci.test_detect_track_density -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.tools.imd import detect_track_density_mismatch, parse_imd_tracks

# =============================================================================
# IMD builder helpers
# =============================================================================

def _make_imd(tracks: list[dict]) -> bytes:
    """
    Build a minimal synthetic IMD file from a list of track dicts.

    Each track dict must have:
        mode      int   IMD mode byte (0-2=FM, 3-5=MFM)
        cylinder  int   cylinder number written into the track header
        head      int   (0 or 1)
        sectors   dict  {sector_id: bytes}  — all sectors must be same size,
                        or empty dict for a track with no sectors

    Optional:
        cyl_map   dict  {sector_id: idam_cylinder}; when present, an IMD cylinder
                        map is emitted so sector_cyls can differ from the track
                        header cylinder, matching the real 40-in-80 samples.
    """
    out = b'IMD test\x1A'
    for t in tracks:
        smap = t['sectors']
        ids  = sorted(smap.keys())
        nsec = len(ids)
        cyl_map = t.get('cyl_map')
        if ids:
            sec_bytes = next(iter(smap.values()))
            size_code = {128: 0, 256: 1, 512: 2, 1024: 3, 2048: 4}[len(sec_bytes)]
        else:
            size_code = 2  # 512B placeholder (nsec=0, so never read)

        head = t['head'] | (0x80 if cyl_map else 0x00)
        out += bytes([t['mode'], t['cylinder'], head, nsec, size_code])
        out += bytes(ids)
        if cyl_map:
            out += bytes([cyl_map[sid] for sid in ids])
        for sid in ids:
            out += b'\x01'      # raw sector type
            out += smap[sid]
    return out


def _write_imd(tracks: list[dict]) -> Path:
    """Write a synthetic IMD to a temp file and return its Path."""
    data = _make_imd(tracks)
    fd, path = tempfile.mkstemp(suffix='.imd')
    os.write(fd, data)
    os.close(fd)
    return Path(path)


def _sector(fill: int = 0xAA, size: int = 512) -> bytes:
    return bytes([fill]) * size


def _varied_sector(size: int = 512) -> bytes:
    """Return sector bytes with varying content (not a uniform fill)."""
    return bytes(i % 256 for i in range(size))


def _one_sided_imd_tracks(cylinders: list[dict]) -> list[dict]:
    """
    Expand per-cylinder head-0 track specs into the IMD layout seen in practice.

    HxC emits one record per (cylinder, head).  For the one-sided samples used by
    the detector, head 0 carries the data and head 1 is present but empty.
    """
    tracks = []
    for cyl, spec in enumerate(cylinders):
        tracks.append({
            'mode': spec.get('mode', 3),
            'cylinder': cyl,
            'head': 0,
            'sectors': spec['sectors'],
            **({'cyl_map': spec['cyl_map']} if 'cyl_map' in spec else {}),
        })
        tracks.append({
            'mode': spec.get('mode', 3),
            'cylinder': cyl,
            'head': 1,
            'sectors': {},
        })
    return tracks


def _two_sided_imd_tracks(cylinders: list[dict]) -> list[dict]:
    """
    Expand per-cylinder specs for both heads into IMD track records.

    Each cylinder spec may contain:
        head0: {'sectors': ..., 'cyl_map': ...}
        head1: {'sectors': ..., 'cyl_map': ...}
    Missing heads default to an empty track.
    """
    tracks = []
    for cyl, spec in enumerate(cylinders):
        for head in (0, 1):
            head_spec = spec.get(f'head{head}', {})
            tracks.append({
                'mode': head_spec.get('mode', 3),
                'cylinder': cyl,
                'head': head,
                'sectors': head_spec.get('sectors', {}),
                **({'cyl_map': head_spec['cyl_map']} if 'cyl_map' in head_spec else {}),
            })
    return tracks


def _even_mismatch_tracks(n_cylinders: int = 80) -> list[dict]:
    """
    Build IMD tracks simulating a 40-track disc read in an 80-track drive.

    On the data-bearing head:
    Even physical cylinder N: one sector with IDAM cylinder = N // 2.
    Odd physical cylinder N:  no sectors (between-track read).

    Head 1 is emitted but empty, matching the real HxC-generated IMDs.
    """
    cylinders = []
    for i in range(n_cylinders):
        if i % 2 == 0:
            cylinders.append({'mode': 3, 'sectors': {1: _sector()}, 'cyl_map': {1: i // 2}})
        else:
            cylinders.append({'mode': 3, 'sectors': {}})
    return _one_sided_imd_tracks(cylinders)


def _normal_tracks(n_cylinders: int) -> list[dict]:
    """Build one-sided IMD tracks for a normal disc where cylinder IDs match."""
    return _one_sided_imd_tracks([
        {'mode': 3, 'sectors': {1: _sector()}}
        for _ in range(n_cylinders)
    ])


def _reformat_tracks(n_cylinders: int = 80, odd_fill: int | None = None) -> list[dict]:
    """
    Build IMD tracks simulating a disc reformatted from 80-track to 40-track.

    On the data-bearing head:
    Even physical cylinder N: new 40-track data, so IDAM cylinder = N // 2.
    Odd physical cylinder N:  old 80-track data remains, so IDAM cylinder = N.
        odd_fill=None  → varied data (real files on old tracks)
        odd_fill=int   → uniform fill byte (formatted-empty leftover tracks)
    """
    cylinders = []
    for i in range(n_cylinders):
        if i % 2 == 0:
            cylinders.append({'mode': 3, 'sectors': {1: _varied_sector()}, 'cyl_map': {1: i // 2}})
        else:
            sec = _sector(fill=odd_fill) if odd_fill is not None else _varied_sector()
            cylinders.append({'mode': 3, 'sectors': {1: sec}, 'cyl_map': {1: i}})
    return _one_sided_imd_tracks(cylinders)


def _double_sided_even_mismatch_tracks(n_cylinders: int = 80) -> list[dict]:
    """Build a double-sided 40-in-80 image with odd tracks unreadable on both sides."""
    cylinders = []
    for i in range(n_cylinders):
        if i % 2 == 0:
            cyl_spec = {
                'head0': {'mode': 3, 'sectors': {1: _sector(fill=0xA1)}, 'cyl_map': {1: i // 2}},
                'head1': {'mode': 3, 'sectors': {1: _sector(fill=0xB2)}, 'cyl_map': {1: i // 2}},
            }
        else:
            cyl_spec = {
                'head0': {'mode': 3, 'sectors': {}},
                'head1': {'mode': 3, 'sectors': {}},
            }
        cylinders.append(cyl_spec)
    return _two_sided_imd_tracks(cylinders)


def _alignment_duplicate_tracks(n_cylinders: int = 80) -> list[dict]:
    """Build a one-sided 40-in-80 image where both half-steps decode the same 40-track data."""
    cylinders = []
    for i in range(n_cylinders):
        cylinders.append({
            'mode': 3,
            'sectors': {1: _varied_sector() if i % 2 == 0 else _sector(fill=0xC3)},
            'cyl_map': {1: i // 2},
        })
    return _one_sided_imd_tracks(cylinders)


def _double_sided_reformat_tracks(n_cylinders: int = 80, odd_fill: int | None = None) -> list[dict]:
    """Build a double-sided 40-in-80 image with odd tracks retained on both sides."""
    cylinders = []
    for i in range(n_cylinders):
        if i % 2 == 0:
            cyl_spec = {
                'head0': {'mode': 3, 'sectors': {1: _varied_sector()}, 'cyl_map': {1: i // 2}},
                'head1': {'mode': 3, 'sectors': {1: _varied_sector()}, 'cyl_map': {1: i // 2}},
            }
        else:
            sec0 = _sector(fill=odd_fill) if odd_fill is not None else _varied_sector()
            sec1 = _sector(fill=odd_fill) if odd_fill is not None else _varied_sector()
            cyl_spec = {
                'head0': {'mode': 3, 'sectors': {1: sec0}, 'cyl_map': {1: i}},
                'head1': {'mode': 3, 'sectors': {1: sec1}, 'cyl_map': {1: i}},
            }
        cylinders.append(cyl_spec)
    return _two_sided_imd_tracks(cylinders)


def _double_sided_alignment_duplicate_tracks(n_cylinders: int = 80) -> list[dict]:
    """Build a double-sided 40-in-80 image where both half-steps decode the same 40-track data."""
    cylinders = []
    for i in range(n_cylinders):
        cyl_spec = {
            'head0': {
                'mode': 3,
                'sectors': {1: _varied_sector() if i % 2 == 0 else _sector(fill=0xC3)},
                'cyl_map': {1: i // 2},
            },
            'head1': {
                'mode': 3,
                'sectors': {1: _varied_sector() if i % 2 == 0 else _sector(fill=0xD4)},
                'cyl_map': {1: i // 2},
            },
        }
        cylinders.append(cyl_spec)
    return _two_sided_imd_tracks(cylinders)


# =============================================================================
# Tests
# =============================================================================

class TestParseImdTracks(unittest.TestCase):

    def test_returns_correct_physical_index(self):
        path = _write_imd([
            {'mode': 3, 'cylinder': i, 'head': 0, 'sectors': {1: _sector()}}
            for i in range(5)
        ])
        try:
            tracks = parse_imd_tracks(path)
            self.assertIsNotNone(tracks)
            self.assertEqual(len(tracks), 5)
            for i, t in enumerate(tracks):
                self.assertEqual(t['physical_index'], i)
        finally:
            os.unlink(path)

    def test_has_data_true_when_sectors_present(self):
        path = _write_imd(_normal_tracks(2))
        try:
            tracks = parse_imd_tracks(path)
            self.assertTrue(tracks[0]['has_data'])
        finally:
            os.unlink(path)

    def test_has_data_false_for_empty_track(self):
        tracks_spec = [
            {'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': {}},
        ]
        path = _write_imd(tracks_spec)
        try:
            tracks = parse_imd_tracks(path)
            self.assertIsNotNone(tracks)
            self.assertFalse(tracks[0]['has_data'])
        finally:
            os.unlink(path)

    def test_sector_cyls_defaults_to_cylinder_when_no_cyl_map(self):
        path = _write_imd([{'mode': 3, 'cylinder': 7, 'head': 0,
                             'sectors': {1: _sector(), 2: _sector()}}])
        try:
            tracks = parse_imd_tracks(path)
            self.assertEqual(tracks[0]['sector_cyls'], [7, 7])
        finally:
            os.unlink(path)

    def test_returns_none_for_non_imd_data(self):
        fd, path = tempfile.mkstemp(suffix='.imd')
        os.write(fd, b'not an IMD file at all')
        os.close(fd)
        try:
            self.assertIsNone(parse_imd_tracks(Path(path)))
        finally:
            os.unlink(path)

    def test_is_uniform_fill_true_for_uniform_sector(self):
        path = _write_imd([{'mode': 3, 'cylinder': 0, 'head': 0,
                             'sectors': {1: _sector(fill=0xE5)}}])
        try:
            tracks = parse_imd_tracks(path)
            self.assertTrue(tracks[0]['is_uniform_fill'])
        finally:
            os.unlink(path)

    def test_is_uniform_fill_false_for_varied_sector(self):
        path = _write_imd([{'mode': 3, 'cylinder': 0, 'head': 0,
                             'sectors': {1: _varied_sector()}}])
        try:
            tracks = parse_imd_tracks(path)
            self.assertFalse(tracks[0]['is_uniform_fill'])
        finally:
            os.unlink(path)

    def test_is_uniform_fill_true_for_empty_track(self):
        path = _write_imd([{'mode': 3, 'cylinder': 0, 'head': 0, 'sectors': {}}])
        try:
            tracks = parse_imd_tracks(path)
            self.assertTrue(tracks[0]['is_uniform_fill'])
        finally:
            os.unlink(path)

    def test_is_uniform_fill_false_when_fill_bytes_differ_across_sectors(self):
        # Two sectors with different fill bytes → not uniform at track level
        path = _write_imd([{'mode': 3, 'cylinder': 0, 'head': 0,
                             'sectors': {1: _sector(fill=0xAA),
                                         2: _sector(fill=0xBB)}}])
        try:
            tracks = parse_imd_tracks(path)
            self.assertFalse(tracks[0]['is_uniform_fill'])
        finally:
            os.unlink(path)


class TestDetectTrackDensityMismatch(unittest.TestCase):

    def _run(self, tracks_spec):
        path = _write_imd(tracks_spec)
        try:
            tracks = parse_imd_tracks(path)
            return detect_track_density_mismatch(tracks)
        finally:
            os.unlink(path)

    def test_density_mismatch_detected(self):
        result = self._run(_even_mismatch_tracks(80))
        self.assertTrue(result['detected'])
        self.assertGreaterEqual(result['confidence'], 0.9)
        self.assertEqual(result['checked'], 40)   # 40 even cylinders with data
        self.assertEqual(result['matching'], 40)

    def test_genuine_80_track_not_detected(self):
        result = self._run(_normal_tracks(80))
        self.assertFalse(result['detected'])

    def test_genuine_40_track_not_detected(self):
        result = self._run(_normal_tracks(40))
        self.assertFalse(result['detected'])

    def test_insufficient_tracks_not_detected(self):
        # Only 4 even cylinders with data (8 cylinders total) — below minimum 6
        result = self._run(_even_mismatch_tracks(8))
        self.assertFalse(result['detected'])
        self.assertEqual(result['checked'], 4)

    def test_partial_match_below_threshold(self):
        # 8 even cylinders with data, 4 matching (50%) — below 70% threshold
        cylinders = []
        for i in range(16):
            if i % 2 == 0:
                # First 4 even cylinders match; next 4 use a wrong IDAM cylinder.
                idam_cyl = i // 2 if i < 8 else 99
                cylinders.append({'mode': 3, 'sectors': {1: _sector()},
                                  'cyl_map': {1: idam_cyl}})
            else:
                cylinders.append({'mode': 3, 'sectors': {}})
        result = self._run(_one_sided_imd_tracks(cylinders))
        self.assertFalse(result['detected'])
        self.assertEqual(result['checked'], 8)
        self.assertEqual(result['matching'], 4)

    def test_partial_match_above_threshold(self):
        # 8 even cylinders with data, 6 matching (75%) — above 70% threshold
        cylinders = []
        for i in range(16):
            if i % 2 == 0:
                # First 6 even cylinders match; last 2 do not.
                idam_cyl = i // 2 if i < 12 else 99
                cylinders.append({'mode': 3, 'sectors': {1: _sector()},
                                  'cyl_map': {1: idam_cyl}})
            else:
                cylinders.append({'mode': 3, 'sectors': {}})
        result = self._run(_one_sided_imd_tracks(cylinders))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 8)
        self.assertEqual(result['matching'], 6)
        self.assertAlmostEqual(result['confidence'], 0.75)

    def test_empty_track_list_returns_not_detected(self):
        result = detect_track_density_mismatch([])
        self.assertFalse(result['detected'])
        self.assertEqual(result['confidence'], 0.0)
        self.assertEqual(result['checked'], 0)

    def test_simple_mismatch_no_odd_track_data(self):
        # Original case: odd cylinders empty → no odd track counts
        result = self._run(_even_mismatch_tracks(80))
        self.assertTrue(result['detected'])
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0])
        self.assertEqual(result['blank_heads'], [1])

    def test_alignment_duplicate_tracks_detected_separately(self):
        result = self._run(_alignment_duplicate_tracks(80))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 40)
        self.assertEqual(result['matching'], 40)
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 40)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0])
        self.assertEqual(result['blank_heads'], [1])

    def test_reformat_mismatch_odd_tracks_have_varied_data(self):
        # Reformat case: odd tracks contain real (non-uniform) 80-track data
        result = self._run(_reformat_tracks(80, odd_fill=None))
        self.assertTrue(result['detected'])
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 40)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0])
        self.assertEqual(result['blank_heads'], [1])

    def test_reformat_mismatch_odd_tracks_have_uniform_fill(self):
        # Reformat case: odd tracks contain formatted-empty leftover sectors
        result = self._run(_reformat_tracks(80, odd_fill=0xE5))
        self.assertTrue(result['detected'])
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 40)
        self.assertEqual(result['data_heads'], [0])
        self.assertEqual(result['blank_heads'], [1])

    def test_double_sided_mismatch_no_odd_track_data(self):
        result = self._run(_double_sided_even_mismatch_tracks(80))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 80)
        self.assertEqual(result['matching'], 80)
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0, 1])
        self.assertEqual(result['blank_heads'], [])

    def test_double_sided_alignment_duplicate_tracks_detected_separately(self):
        result = self._run(_double_sided_alignment_duplicate_tracks(80))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 80)
        self.assertEqual(result['matching'], 80)
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 80)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0, 1])
        self.assertEqual(result['blank_heads'], [])

    def test_double_sided_reformat_mismatch_odd_tracks_have_varied_data(self):
        result = self._run(_double_sided_reformat_tracks(80, odd_fill=None))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 80)
        self.assertEqual(result['matching'], 80)
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 80)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 0)
        self.assertEqual(result['data_heads'], [0, 1])
        self.assertEqual(result['blank_heads'], [])

    def test_double_sided_reformat_mismatch_odd_tracks_have_uniform_fill(self):
        result = self._run(_double_sided_reformat_tracks(80, odd_fill=0xE5))
        self.assertTrue(result['detected'])
        self.assertEqual(result['checked'], 80)
        self.assertEqual(result['matching'], 80)
        self.assertEqual(result['odd_tracks_with_duplicate_data'], 0)
        self.assertEqual(result['odd_tracks_with_varied_data'], 0)
        self.assertEqual(result['odd_tracks_with_uniform_data'], 80)
        self.assertEqual(result['data_heads'], [0, 1])
        self.assertEqual(result['blank_heads'], [])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
