"""The web version helper reads build metadata without spawning Git."""

import unittest
from unittest.mock import mock_open, patch
from myapp import version


class TestVersion(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, version, '_version_cache', None)
        version._version_cache = None

    def test_reads_and_caches_build_metadata_without_processes(self):
        with patch('builtins.open', mock_open(read_data='v1.2-3-gabcdef\n')) as opened, \
             patch('subprocess.Popen', side_effect=AssertionError('web launched a process')):
            self.assertEqual(version.get_version(), 'v1.2-3-gabcdef')
            self.assertEqual(version.get_version(), 'v1.2-3-gabcdef')
            self.assertEqual(opened.call_count, 1)

    def test_missing_or_empty_metadata(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertEqual(version.get_version(), 'unknown')
        version._version_cache = None
        with patch('builtins.open', mock_open(read_data='\n')):
            self.assertEqual(version.get_version(), 'unknown')
