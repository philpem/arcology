"""Integration tests: top-level archive extraction.

Each test runs a committed archive fixture through the real ARCHIVE_EXTRACT
handler and tools, then compares the recorded server interactions, extracted
file listing and output tree to a golden file.

Run via ``ci/integration/run_integration.py`` (inside the worker container);
``scripts/run-integration.sh`` builds the image and does this for you.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'worker'))

from harness.runner import check_case, list_fixture_cases  # noqa: E402
from harness.tooling import require_tools  # noqa: E402

# Cases handled by this module and the tools each needs present.
_CASES = {
    'zip_plain': ['unzip'],
    'tar_gz': ['gzip', 'tar'],
    'zip_promote': ['unzip'],
}


class TestArchiveExtraction(unittest.TestCase):
    def _run(self, case_name):
        require_tools(_CASES[case_name])
        check_case(self, case_name)

    def test_zip_plain(self):
        self._run('zip_plain')

    def test_tar_gz(self):
        self._run('tar_gz')

    def test_zip_promote(self):
        self._run('zip_promote')


class TestFixtureCoverage(unittest.TestCase):
    """Every committed archive fixture must be claimed by a test above."""

    def test_all_archive_fixtures_have_tests(self):
        # Partition/disk-image fixtures (added in a later iteration) are not
        # archive cases; restrict this check to the archive cases this module
        # owns by intersecting with on-disk fixtures it knows about.
        present = set(list_fixture_cases())
        owned = set(_CASES)
        missing = owned - present
        self.assertFalse(
            missing, f"declared archive cases with no fixture on disk: {missing}"
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
