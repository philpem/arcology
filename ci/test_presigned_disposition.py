"""
Tests for safe Content-Disposition construction in S3 pre-signed URLs.

The download endpoints pass an artefact's *original_filename* to
``S3Storage.presigned_url(..., filename=...)``, which sets the S3
``ResponseContentDisposition`` override.  S3 reproduces that value verbatim in
the ``Content-Disposition`` response header when the object is fetched.

The web upload sanitiser (``safe_original_filename``) deliberately preserves
most characters — including double-quote and even CR/LF — so an unsanitised
filename interpolated straight into ``attachment; filename="<name>"`` is a
response-header-injection vector (CWE-113).  These tests prove the original
filename can carry such characters and verify the disposition builder
neutralises them.

Run:
    python -m unittest ci.test_presigned_disposition -v
"""

import os
import sys
import unittest
import urllib.parse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# A filename an attacker could choose at upload time: it breaks out of the
# quoted filename and attempts to inject a second response header via CRLF.
_EVIL = 'pwn".pdf\r\nX-Injected: 1\r\nContent-Type: text/html'


class TestUploadSanitiserPreservesUnsafeChars(unittest.TestCase):
    """Establish the precondition: the upload sanitiser keeps " and CR/LF."""

    def test_safe_original_filename_keeps_quote_and_crlf(self):
        os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
        os.environ.setdefault('SECRET_KEY', 'ci-presigned-test-key')
        os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
        from myapp.blueprints.artefacts import safe_original_filename
        out = safe_original_filename(_EVIL)
        self.assertIn('"', out)
        self.assertTrue('\r' in out or '\n' in out)


class TestContentDispositionBuilder(unittest.TestCase):
    """The disposition builder must neutralise header-injection characters."""

    def setUp(self):
        from shared.storage import _content_disposition_attachment
        self.build = _content_disposition_attachment

    def test_no_raw_control_chars_or_unescaped_quote(self):
        value = self.build(_EVIL)
        # No raw CR/LF/NUL/backslash anywhere in the header value.
        for ch in ('\r', '\n', '\x00', '\\'):
            self.assertNotIn(ch, value)
        # Exactly two double-quotes: the pair around the ASCII filename.
        self.assertEqual(value.count('"'), 2, value)
        # Still an attachment, and the original name survives (encoded) for
        # capable browsers via RFC 5987.
        self.assertTrue(value.startswith('attachment; filename="'))
        self.assertIn("filename*=UTF-8''", value)

    def test_plain_filename_passthrough(self):
        value = self.build('report.pdf')
        self.assertIn('filename="report.pdf"', value)

    def test_control_chars_only_is_neutralised(self):
        # An all-control-char name is replaced char-for-char; still injection-safe.
        value = self.build('\r\n\x00')
        for ch in ('\r', '\n', '\x00'):
            self.assertNotIn(ch, value)
        self.assertTrue(value.startswith('attachment; filename="'))

    def test_non_ascii_name_falls_back_for_ascii_param(self):
        # Pure non-ASCII name (no ASCII chars at all): the ASCII filename=
        # falls back to 'download', while filename* still carries the original
        # via RFC 5987 percent-encoding.
        name = '日本語'
        value = self.build(name)
        self.assertIn('filename="download"', value)
        self.assertIn("filename*=UTF-8''" + urllib.parse.quote(name, safe=''), value)


class TestPresignedUrlNoInjection(unittest.TestCase):
    """End-to-end: the signed URL's disposition param carries no raw CRLF."""

    def setUp(self):
        try:
            import boto3  # noqa: F401
        except ImportError:
            self.skipTest('boto3 not installed')
        from shared.storage import S3Storage
        # generate_presigned_url signs locally; no network call is made.
        self.storage = S3Storage(
            endpoint_url='http://s3.local:9000', bucket='b',
            access_key='AKIAEXAMPLE', secret_key='secret', region='us-east-1',
        )

    def test_disposition_param_has_no_crlf(self):
        url = self.storage.presigned_url('uploads/abc.pdf', filename=_EVIL)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        disp = query.get('response-content-disposition', [''])[0]
        # The decoded signed value is what S3 echoes into the response header.
        self.assertNotIn('\r', disp)
        self.assertNotIn('\n', disp)
        # The attacker's injected header text is no longer a standalone header:
        # it survives only percent-encoded inside the RFC 5987 filename*.
        self.assertNotIn('\r\nX-Injected', disp)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
