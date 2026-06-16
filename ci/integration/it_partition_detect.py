"""Integration tests: partition / filesystem detection.

Runs the real PARTITION_DETECT handler (and its detectors: pure-Python ADFS/FAT
parsing plus sfdisk and file(1)) against committed disc-image fixtures, and
compares the recorded interactions and queued follow-ups to a golden.

Run via ``ci/integration/run_integration.py`` (inside the worker container);
``scripts/run-integration.sh`` builds the image and does this for you.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from harness.runner import check_case  # noqa: E402
from harness.tooling import require_tools  # noqa: E402

# Cases handled by this module and the tools each needs present.
_CASES = {
    # Whole-disc FAT12: identified by pure-Python BPB parsing; sfdisk reports
    # no partition table and the file(1) clause is normalised away.  Detection
    # queues FILE_EXTRACTION, which is asserted in final_queue (not executed).
    'fat_720k': ['sfdisk', 'file'],
    # MBR with two partitions (FAT16 + FAT32): sfdisk reads the table, each
    # partition is carved into a derived RAW_SECTOR artefact with FILE_EXTRACTION
    # queued; uniform zero gaps are omitted with a note, the MBR-bearing
    # pre-partition gap is registered as an UNKNOWN artefact.
    'mbr_2part': ['sfdisk', 'file'],
}


class TestPartitionDetect(unittest.TestCase):
    def _run(self, case_name):
        require_tools(_CASES[case_name])
        check_case(self, case_name)

    def test_fat_720k(self):
        self._run('fat_720k')

    def test_mbr_2part(self):
        self._run('mbr_2part')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
