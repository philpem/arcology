"""
Tests for file-timestamp preservation in the extraction pipeline.

Covers:
- enumerate_extracted_files() populating modified_time from st_mtime
- INF sidecar modified_time overriding the filesystem mtime
- Directories do not get a modified_time entry

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
from datetime import datetime
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
