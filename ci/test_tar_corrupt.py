"""
Regression test: a corrupt/truncated tarball must produce a failure result
dict from extract_tar(), not an unhandled tarfile.ReadError that crashes the
analysis job.  The path-safety pre-check opens the archive with tarfile, which
raises ReadError (not ValueError) on malformed input.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestCorruptTar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out_dir = self.tmp / 'out'

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupt_tar_returns_failure_result(self):
        from worker.arcworker.tools.archives import extract_tar

        bad_tar = self.tmp / 'corrupt.tar'
        bad_tar.write_bytes(b'this is not a tar archive' * 40)

        result = extract_tar(bad_tar, self.out_dir)

        self.assertFalse(result['success'])
        self.assertEqual(result['tool'], 'tar')
        self.assertIn('TAR', result['error'])

    def test_truncated_tar_returns_failure_result(self):
        import tarfile
        from worker.arcworker.tools.archives import extract_tar

        good_tar = self.tmp / 'good.tar'
        member = self.tmp / 'member.txt'
        member.write_text('hello')
        with tarfile.open(good_tar, 'w') as tf:
            tf.add(member, arcname='member.txt')

        truncated = self.tmp / 'truncated.tar'
        truncated.write_bytes(good_tar.read_bytes()[:100])

        result = extract_tar(truncated, self.out_dir)

        self.assertFalse(result['success'])
        self.assertEqual(result['tool'], 'tar')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
