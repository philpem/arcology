"""
Tests for file-timestamp preservation in the extraction pipeline.

Covers:
- enumerate_extracted_files() populating modified_time from st_mtime
- INF sidecar modified_time overriding the filesystem mtime
- Directories do not get a modified_time entry
- Rejecting fabricated/invalid timestamps: extraction-window "now" dates
  (sources with no real timestamp, e.g. BBC DFS), future dates, and dates
  that predate every catalogued platform

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_timestamps -v
"""

import calendar
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


from worker.arcworker.tools.extraction import enumerate_extracted_files


class TestEnumerateExtractedFilesTimestamp(unittest.TestCase):
    """enumerate_extracted_files() should populate modified_time."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_modified_time_present(self):
        """Each regular file entry includes a modified_time key."""
        (self._tmpdir / 'file.txt').write_bytes(b'hello')
        files = enumerate_extracted_files(self._tmpdir)
        self.assertEqual(len(files), 1)
        self.assertIn('modified_time', files[0])

    def test_modified_time_is_iso_string(self):
        """modified_time is a parseable ISO 8601 string."""
        (self._tmpdir / 'file.txt').write_bytes(b'hello')
        files = enumerate_extracted_files(self._tmpdir)
        dt = datetime.fromisoformat(files[0]['modified_time'])
        self.assertIsInstance(dt, datetime)

    def test_modified_time_reflects_mtime(self):
        """modified_time matches the file's st_mtime (to the second)."""
        f = self._tmpdir / 'file.txt'
        f.write_bytes(b'test')
        stat_mtime = f.stat().st_mtime
        expected = datetime.utcfromtimestamp(stat_mtime).replace(microsecond=0)

        files = enumerate_extracted_files(self._tmpdir)
        got = datetime.fromisoformat(files[0]['modified_time']).replace(microsecond=0)
        self.assertEqual(got, expected)

    def test_directory_entries_have_no_modified_time(self):
        """Empty-directory sentinel entries do not carry a modified_time."""
        empty_dir = self._tmpdir / 'empty'
        empty_dir.mkdir()
        files = enumerate_extracted_files(self._tmpdir)
        dirs = [f for f in files if f.get('is_directory')]
        self.assertTrue(len(dirs) >= 1)
        for d in dirs:
            self.assertNotIn('modified_time', d)

    def test_inf_modified_time_overrides_mtime(self):
        """INF-supplied RISC OS timestamp overwrites the filesystem mtime."""
        f = self._tmpdir / 'MYFILE,ffd'
        f.write_bytes(b'data')

        # Build a RISC OS timestamp for 1994-10-01 00:00:00 UTC
        expected_dt = datetime(1994, 10, 1, 0, 0, 0)
        cs = calendar.timegm(expected_dt.timetuple()) * 100 + 220898880000
        high = (cs >> 32) & 0xFF
        exec_ = cs & 0xFFFFFFFF
        load = 0xFFFFFD00 | high  # FD filetype

        inf_metadata = {
            'MYFILE': {
                'load_address': f'{load:08x}',
                'exec_address': f'{exec_:08x}',
                'risc_os_filetype': 'ffd',
                'modified_time': expected_dt.isoformat(),
            }
        }
        files = enumerate_extracted_files(
            self._tmpdir, acorn=True, inf_metadata=inf_metadata
        )
        # Only one file
        regular = [f for f in files if not f.get('is_directory')]
        self.assertEqual(len(regular), 1)
        self.assertEqual(regular[0]['modified_time'], expected_dt.isoformat())

    def test_inf_without_modified_time_keeps_mtime(self):
        """INF metadata without modified_time leaves the filesystem mtime intact."""
        f = self._tmpdir / 'MYFILE,ffd'
        f.write_bytes(b'data')
        stat_mtime = f.stat().st_mtime

        inf_metadata = {
            'MYFILE': {
                'load_address': 'fffffd00',
                'exec_address': '00000000',
                'risc_os_filetype': 'ffd',
                # No modified_time key
            }
        }
        files = enumerate_extracted_files(
            self._tmpdir, acorn=True, inf_metadata=inf_metadata
        )
        regular = [f for f in files if not f.get('is_directory')]
        self.assertEqual(len(regular), 1)
        got = datetime.fromisoformat(regular[0]['modified_time']).replace(microsecond=0)
        expected = datetime.utcfromtimestamp(stat_mtime).replace(microsecond=0)
        self.assertEqual(got, expected)


class TestInvalidTimestampRejection(unittest.TestCase):
    """Fabricated and impossible timestamps are dropped to 'unknown' (null)."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _set_mtime(self, path, dt):
        ts = calendar.timegm(dt.timetuple())
        os.utime(path, (ts, ts))

    def test_extraction_window_mtime_dropped(self):
        """A freshly-written file (mtime == now) is dropped when a start time is
        supplied — this is the tool's fabricated 'today' for a source with no
        real date (e.g. BBC DFS)."""
        (self._tmpdir / 'file.txt').write_bytes(b'data')
        started = datetime.now(timezone.utc) - timedelta(seconds=1)
        files = enumerate_extracted_files(
            self._tmpdir, extraction_started_at=started
        )
        self.assertEqual(len(files), 1)
        self.assertNotIn('modified_time', files[0])

    def test_genuine_old_mtime_kept_with_start(self):
        """A real, old mtime survives even when a start time is supplied."""
        f = self._tmpdir / 'file.txt'
        f.write_bytes(b'data')
        self._set_mtime(f, datetime(1994, 6, 1))
        started = datetime.now(timezone.utc)
        files = enumerate_extracted_files(
            self._tmpdir, extraction_started_at=started
        )
        self.assertEqual(len(files), 1)
        got = datetime.fromisoformat(files[0]['modified_time'])
        self.assertEqual(got.year, 1994)

    def test_future_mtime_dropped(self):
        """A timestamp in the future is impossible and dropped (no start time
        needed)."""
        f = self._tmpdir / 'file.txt'
        f.write_bytes(b'data')
        self._set_mtime(f, datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(days=3650))
        files = enumerate_extracted_files(self._tmpdir)
        self.assertEqual(len(files), 1)
        self.assertNotIn('modified_time', files[0])

    def test_prehistoric_mtime_dropped(self):
        """A timestamp before the platform-era floor (here: Unix epoch) is
        treated as a corrupt/sentinel value and dropped."""
        f = self._tmpdir / 'file.txt'
        f.write_bytes(b'data')
        self._set_mtime(f, datetime(1970, 1, 1))
        files = enumerate_extracted_files(self._tmpdir)
        self.assertEqual(len(files), 1)
        self.assertNotIn('modified_time', files[0])

    def test_inf_date_kept_despite_start(self):
        """A genuine INF-decoded RISC OS date survives the window check (it is
        an old date, not the extraction 'now')."""
        f = self._tmpdir / 'MYFILE,ffd'
        f.write_bytes(b'data')
        expected_dt = datetime(1994, 10, 1, 0, 0, 0)
        inf_metadata = {
            'MYFILE': {
                'risc_os_filetype': 'ffd',
                'modified_time': expected_dt.replace(
                    tzinfo=timezone.utc).isoformat(),
            }
        }
        started = datetime.now(timezone.utc)
        files = enumerate_extracted_files(
            self._tmpdir, acorn=True, inf_metadata=inf_metadata,
            extraction_started_at=started,
        )
        regular = [f for f in files if not f.get('is_directory')]
        self.assertEqual(len(regular), 1)
        got = datetime.fromisoformat(regular[0]['modified_time'])
        self.assertEqual(got.year, 1994)

    def test_corrupt_inf_future_date_dropped(self):
        """A corrupt INF decode yielding an absurd future year is dropped."""
        f = self._tmpdir / 'MYFILE,ffd'
        f.write_bytes(b'data')
        inf_metadata = {
            'MYFILE': {
                'risc_os_filetype': 'ffd',
                'modified_time': datetime(
                    5000, 1, 1, tzinfo=timezone.utc).isoformat(),
            }
        }
        files = enumerate_extracted_files(
            self._tmpdir, acorn=True, inf_metadata=inf_metadata,
        )
        regular = [f for f in files if not f.get('is_directory')]
        self.assertEqual(len(regular), 1)
        self.assertNotIn('modified_time', regular[0])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
