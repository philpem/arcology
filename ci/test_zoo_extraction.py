"""Security regression tests for Zoo archive extraction."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestZooExtraction(unittest.TestCase):
    def test_extractor_fails_closed_without_running_zoo(self):
        from worker.arcworker.tools import archives

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            archives, '_run_extraction_command'
        ) as run_extractor:
            result = archives.extract_zoo(Path(tmp) / 'hostile.zoo', Path(tmp) / 'out')

        self.assertFalse(result['success'])
        self.assertEqual(result['tool'], 'zoo')
        self.assertIn('disabled', result['error'])
        run_extractor.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
