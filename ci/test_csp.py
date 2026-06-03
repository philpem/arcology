"""
Content-Security-Policy tests.

The default CSP uses `img-src 'self' data:`.  When S3 storage is configured
with a browser-reachable public URL on a different origin, output files
(visualisations, thumbnails) are served by redirecting <img> requests to
pre-signed URLs on that origin.  Browsers re-check CSP against each redirect
hop, so the S3 origin must be whitelisted in img-src/media-src or every
visualisation is blocked.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_csp -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-csp-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestS3PublicOrigin(unittest.TestCase):
    """_s3_public_origin() reduces a configured S3 URL to its CSP origin."""

    def _origin(self, **config):
        from myapp.app import _s3_public_origin
        return _s3_public_origin(config)

    def test_local_storage_returns_none(self):
        self.assertIsNone(self._origin(STORAGE_BACKEND='local'))
        # Default (key absent) is local.
        self.assertIsNone(self._origin())

    def test_s3_without_url_returns_none(self):
        self.assertIsNone(self._origin(STORAGE_BACKEND='s3'))

    def test_s3_public_url_origin(self):
        self.assertEqual(
            self._origin(STORAGE_BACKEND='s3',
                         S3_PUBLIC_URL='https://arco-s3.example.com'),
            'https://arco-s3.example.com',
        )

    def test_public_url_with_path_is_reduced_to_origin(self):
        # A path/query is dropped — CSP matches on origin, so the bare origin
        # whitelists every pre-signed object URL (including a path prefix).
        self.assertEqual(
            self._origin(STORAGE_BACKEND='s3',
                         S3_PUBLIC_URL='https://example.com/s3/bucket?x=1'),
            'https://example.com',
        )

    def test_public_url_with_port_is_preserved(self):
        self.assertEqual(
            self._origin(STORAGE_BACKEND='s3',
                         S3_PUBLIC_URL='http://localhost:3900'),
            'http://localhost:3900',
        )

    def test_falls_back_to_endpoint_url(self):
        self.assertEqual(
            self._origin(STORAGE_BACKEND='s3',
                         S3_ENDPOINT_URL='https://garage.example.com'),
            'https://garage.example.com',
        )

    def test_public_url_preferred_over_endpoint(self):
        self.assertEqual(
            self._origin(STORAGE_BACKEND='s3',
                         S3_ENDPOINT_URL='http://garage:3900',
                         S3_PUBLIC_URL='https://public.example.com'),
            'https://public.example.com',
        )

    def test_unparseable_url_returns_none(self):
        # No scheme/netloc → leave the CSP unchanged rather than emit garbage.
        self.assertIsNone(
            self._origin(STORAGE_BACKEND='s3', S3_PUBLIC_URL='not a url')
        )

    def test_backend_name_is_case_insensitive(self):
        self.assertEqual(
            self._origin(STORAGE_BACKEND='S3',
                         S3_PUBLIC_URL='https://s3.example.com'),
            'https://s3.example.com',
        )


class TestCSPHeader(unittest.TestCase):
    """End-to-end: the response header reflects the configured storage."""

    @staticmethod
    def _csp_for(**env_overrides):
        # create_app reads S3 settings from the environment, so set them, build
        # an app, fetch a page, then restore the environment.
        saved = {}
        keys = ('STORAGE_BACKEND', 'S3_ENDPOINT_URL', 'S3_PUBLIC_URL',
                'S3_BUCKET', 'S3_ACCESS_KEY', 'S3_SECRET_KEY')
        try:
            for k in keys:
                saved[k] = os.environ.get(k)
                os.environ.pop(k, None)
            for k, v in env_overrides.items():
                os.environ[k] = v
            from myapp.app import create_app
            app = create_app()
            resp = app.test_client().get('/login')
            return resp.headers.get('Content-Security-Policy', '')
        finally:
            for k in keys:
                if saved[k] is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = saved[k]

    def test_local_storage_has_no_external_img_origin(self):
        csp = self._csp_for(STORAGE_BACKEND='local')
        self.assertIn("img-src 'self' data:;", csp)
        self.assertIn("media-src 'self';", csp)
        self.assertNotIn('https://arco-s3', csp)

    def test_external_s3_origin_in_img_and_media_src(self):
        csp = self._csp_for(
            STORAGE_BACKEND='s3',
            S3_ENDPOINT_URL='http://garage:3900',
            S3_PUBLIC_URL='https://arco-s3.example.com',
            S3_BUCKET='arcology', S3_ACCESS_KEY='k', S3_SECRET_KEY='s',
        )
        self.assertIn("img-src 'self' data: https://arco-s3.example.com;", csp)
        self.assertIn("media-src 'self' https://arco-s3.example.com;", csp)

    def test_path_in_public_url_yields_origin_only(self):
        csp = self._csp_for(
            STORAGE_BACKEND='s3',
            S3_ENDPOINT_URL='http://garage:3900',
            S3_PUBLIC_URL='https://example.com/s3',
            S3_BUCKET='arcology', S3_ACCESS_KEY='k', S3_SECRET_KEY='s',
        )
        # Origin whitelisted (matches all paths); the /s3 path is not embedded.
        self.assertIn("img-src 'self' data: https://example.com;", csp)
        self.assertNotIn('https://example.com/s3', csp)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
