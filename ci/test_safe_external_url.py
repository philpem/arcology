"""
Tests that user-supplied external URLs cannot inject a javascript: href.

ExternalReference.url (external-system links) and HashDatabase.safe_source_url
are rendered into <a href> on public/authenticated pages.  Both gate their
value through safe_external_url(), which permits http/https and relative URLs
but strips script-executing pseudo-schemes.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_safe_external_url -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-safe-url-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestSafeExternalUrl(unittest.TestCase):
    """The pure predicate."""

    def _safe(self, url):
        from myapp.utils.urls import safe_external_url
        return safe_external_url(url)

    def test_dangerous_schemes_rejected(self):
        for bad in (
            'javascript:alert(1)',
            'JavaScript:alert(1)',
            '  javascript:alert(1)',      # leading whitespace browsers ignore
            'java\tscript:alert(1)',      # tab inside the scheme
            'data:text/html,<script>x</script>',
            'vbscript:msgbox(1)',
            'file:///etc/passwd',
        ):
            self.assertIsNone(self._safe(bad), f'{bad!r} must be rejected')

    def test_safe_urls_passed_through(self):
        for ok in (
            'https://example.com/x',
            'http://example.com/x?y=1',
            '/relative/path',
            '//scheme-relative.example.com/x',
            'items/42',
        ):
            self.assertEqual(self._safe(ok), ok)

    def test_empty_is_none(self):
        self.assertIsNone(self._safe(''))
        self.assertIsNone(self._safe(None))


class TestModelUrlProperties(unittest.TestCase):
    """The two model properties that feed <a href>."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        cls.app = create_app()

    def test_external_reference_url_strips_javascript(self):
        from myapp.database import ExternalReference, ExternalSystem
        with self.app.app_context():
            evil = ExternalReference(external_id='123')
            evil.system = ExternalSystem(name='S', base_url='javascript:alert(1)//',
                                         url_template='{id}')
            self.assertIsNone(evil.url)

            good = ExternalReference(external_id='123')
            good.system = ExternalSystem(name='S', base_url='https://cat.example.com/',
                                         url_template='item/{id}')
            self.assertEqual(good.url, 'https://cat.example.com/item/123')

    def test_hashdatabase_safe_source_url(self):
        from myapp.database import HashDatabase
        self.assertIsNone(HashDatabase(name='D', source_url='javascript:alert(1)').safe_source_url)
        self.assertEqual(
            HashDatabase(name='D', source_url='https://nist.gov/rds').safe_source_url,
            'https://nist.gov/rds')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
