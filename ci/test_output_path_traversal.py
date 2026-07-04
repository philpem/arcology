"""
Tests that analysis-output serving rejects path-traversing filenames.

output_access_decision() authorises a request by the artefact UUID embedded in
the *second* path component, but serve_output_file() serves the realpath of the
filename (confined only to the outputs root).  A '..' segment collapses to a
different artefact's directory at serve time, so a traversing path could
authorise as a viewable artefact yet serve a private/restricted artefact's
outputs.  Both entry points now reject any path with an empty / '.' / '..'
segment via is_safe_output_path().

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_output_path_traversal -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-output-traversal-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestIsSafeOutputPath(unittest.TestCase):

    def _safe(self, filename):
        from myapp.services.downloads import is_safe_output_path
        return is_safe_output_path(filename)

    def test_traversing_and_absolute_paths_rejected(self):
        for bad in (
            'item/aaaa_slug/../../other/bbbb_slug/secret.png',
            '../etc/passwd',
            'item/../secret',
            '/abs/path.png',       # leading slash → empty first segment
            'item//double.png',    # empty middle segment
            'item/aaaa_slug/.',
            '',
            None,
        ):
            self.assertFalse(self._safe(bad), f'{bad!r} should be rejected')

    def test_normal_output_paths_allowed(self):
        for ok in (
            'item123/deadbeef_slug/render.png',
            'media/abc123/1/movie.mp4',
            'a/b/c/d.txt',
        ):
            self.assertTrue(self._safe(ok), f'{ok!r} should be allowed')


class TestOutputRoutesRejectTraversal(unittest.TestCase):
    """The two serving entry points both reject a traversing path early."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        cls.app = create_app()

    def test_access_decision_returns_not_found(self):
        from myapp.services.downloads import output_access_decision
        traversal = 'item/aaaa_slug/../../other/bbbb_slug/secret.png'
        with self.app.app_context():
            # Returns before any artefact lookup — no DB rows needed.
            self.assertEqual(output_access_decision(traversal, None), 'not_found')

    def test_serve_output_file_returns_none(self):
        from myapp.services.downloads import serve_output_file
        traversal = 'item/aaaa_slug/../../other/bbbb_slug/secret.png'
        with self.app.app_context():
            self.assertIsNone(serve_output_file(traversal))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
