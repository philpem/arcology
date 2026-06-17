"""
Unit tests for the product-recognition matcher (myapp/services/recognition.py).

These exercise the pure matching rules with no database:
  * required-all / optional-count / optional-only-needs-one
  * path matching strips the folder prefix before comparing
  * best-hash selection SHA-256 -> SHA-1 -> MD5 (issue #620)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_recognition_matcher -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-recognition-matcher-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _index(files):
    """Build a folder_index from ``[(rel, md5, sha1, sha256), ...]``."""
    idx = {'md5s': set(), 'sha1s': set(), 'sha256s': set(), 'path_map': {}}
    for rel, md5, sha1, sha256 in files:
        md5 = (md5 or '').lower()
        sha1 = (sha1 or '').lower()
        sha256 = (sha256 or '').lower()
        if md5:
            idx['md5s'].add(md5)
        if sha1:
            idx['sha1s'].add(sha1)
        if sha256:
            idx['sha256s'].add(sha256)
        idx['path_map'][rel.lower()] = {'md5': md5, 'sha1': sha1, 'sha256': sha256}
    return idx


def _kf(md5=None, sha1=None, sha256=None, relative_path=None):
    return {'md5': md5, 'sha1': sha1, 'sha256': sha256, 'relative_path': relative_path}


def _product(required=(), optional=(), path_match_enabled=False):
    return {
        'path_match_enabled': path_match_enabled,
        'required_files': list(required),
        'optional_files': list(optional),
    }


class TestSelectBestHash(unittest.TestCase):
    def test_prefers_sha256_then_sha1_then_md5(self):
        from myapp.services.recognition import select_best_hash
        self.assertEqual(select_best_hash('aa', 'bb', 'cc'), ('sha256', 'cc'))
        self.assertEqual(select_best_hash('aa', 'bb', None), ('sha1', 'bb'))
        self.assertEqual(select_best_hash('aa', None, None), ('md5', 'aa'))
        self.assertEqual(select_best_hash(None, None, None), (None, None))

    def test_lowercases_value(self):
        from myapp.services.recognition import select_best_hash
        self.assertEqual(select_best_hash(None, None, 'ABCdef'), ('sha256', 'abcdef'))


class TestVerifyProductInFolder(unittest.TestCase):
    def setUp(self):
        from myapp.services.recognition import verify_product_in_folder
        self.verify = verify_product_in_folder

    def test_all_required_present(self):
        idx = _index([('run', 'aa', None, None), ('data', 'bb', None, None)])
        product = _product(required=[_kf(md5='AA'), _kf(md5='BB')])
        self.assertEqual(self.verify(product, idx, ''), (2, 2, 0, 0))

    def test_missing_required_fails(self):
        idx = _index([('run', 'aa', None, None)])
        product = _product(required=[_kf(md5='aa'), _kf(md5='bb')])
        self.assertIsNone(self.verify(product, idx, ''))

    def test_optional_counted_but_not_required(self):
        idx = _index([('run', 'aa', None, None)])
        product = _product(required=[_kf(md5='aa')], optional=[_kf(md5='zz')])
        # required satisfied; optional absent -> still a match, 0 optional.
        self.assertEqual(self.verify(product, idx, ''), (1, 1, 0, 1))

    def test_optional_only_needs_at_least_one(self):
        idx = _index([('run', 'aa', None, None)])
        none_present = _product(optional=[_kf(md5='zz')])
        self.assertIsNone(self.verify(none_present, idx, ''))
        one_present = _product(optional=[_kf(md5='aa'), _kf(md5='zz')])
        self.assertEqual(self.verify(one_present, idx, ''), (0, 0, 1, 2))

    def test_path_match_strips_folder_prefix(self):
        # The known file's relative_path is root-relative ('!App/!Run'); inside
        # folder '!App' the path_map key is just 'run'... mapped from rel '!run'.
        idx = _index([('!Run', 'aa', None, None)])
        product = _product(
            required=[_kf(md5='aa', relative_path='!App/!Run')],
            path_match_enabled=True,
        )
        self.assertEqual(self.verify(product, idx, '!App'), (1, 1, 0, 0))

    def test_path_match_wrong_path_fails(self):
        idx = _index([('!Run', 'aa', None, None)])
        product = _product(
            required=[_kf(md5='aa', relative_path='!App/Other')],
            path_match_enabled=True,
        )
        self.assertIsNone(self.verify(product, idx, '!App'))

    def test_best_hash_uses_sha256_when_present(self):
        # Known file has sha256 + md5; folder file has the matching sha256 but a
        # DIFFERENT md5 -> still matches because sha256 is the best hash.
        idx = _index([('run', 'differentmd5', None, 'cc')])
        product = _product(required=[_kf(md5='aa', sha256='CC')])
        self.assertEqual(self.verify(product, idx, ''), (1, 1, 0, 0))

    def test_best_hash_sha256_required_but_missing_on_file(self):
        # Known file's best hash is sha256; the folder file has no sha256 (only a
        # matching md5) -> no match, since sha256 is required once present.
        idx = _index([('run', 'aa', None, None)])
        product = _product(required=[_kf(md5='aa', sha256='cc')])
        self.assertIsNone(self.verify(product, idx, ''))

    def test_best_hash_falls_back_to_sha1(self):
        idx = _index([('run', None, 'bb', None)])
        product = _product(required=[_kf(sha1='BB')])
        self.assertEqual(self.verify(product, idx, ''), (1, 1, 0, 0))

    def test_empty_product_no_match(self):
        idx = _index([('run', 'aa', None, None)])
        self.assertIsNone(self.verify(_product(), idx, ''))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
