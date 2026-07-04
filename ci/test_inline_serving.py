"""
Tests that the media stream routes only serve non-scriptable content inline.

Serving user-uploaded bytes inline (no Content-Disposition: attachment) renders
them in the browser on the application origin.  An uploaded .html or .svg file
served inline is therefore stored XSS.  serve_artefact_file / serve_extracted_file
gate inline serving through _safe_inline_mimetype(), which only permits genuinely
non-scriptable media (audio/video/raster image) and forces everything else to an
attachment download.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_inline_serving -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-inline-serving-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestSafeInlineMimetype(unittest.TestCase):
    """The allowlist that decides whether a file may be served inline."""

    def _safe(self, filename):
        from myapp.services.downloads import _safe_inline_mimetype
        return _safe_inline_mimetype(filename)

    def test_scriptable_types_are_never_inline(self):
        # These are the stored-XSS vectors: HTML executes script, SVG carries
        # inline <script>, and a bare download must stay an attachment.
        for name in ('evil.html', 'evil.htm', 'evil.svg', 'evil.xhtml',
                     'evil.xml', 'payload.js'):
            self.assertIsNone(self._safe(name),
                              f'{name} must not be served inline')

    def test_media_types_are_served_inline(self):
        self.assertEqual(self._safe('clip.mp4'), 'video/mp4')
        self.assertEqual(self._safe('track.mp3'), 'audio/mpeg')
        self.assertEqual(self._safe('photo.png'), 'image/png')
        self.assertEqual(self._safe('photo.jpg'), 'image/jpeg')
        self.assertEqual(self._safe('photo.gif'), 'image/gif')

    def test_unknown_or_missing_type_falls_back_to_attachment(self):
        self.assertIsNone(self._safe('disk.scp'))
        self.assertIsNone(self._safe('noextension'))
        self.assertIsNone(self._safe(''))
        self.assertIsNone(self._safe(None))


if __name__ == '__main__':
    unittest.main()
