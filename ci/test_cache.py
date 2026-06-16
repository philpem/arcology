"""
Version-keyed cache tests.

Verifies the correctness-critical behaviour of myapp/services/cache.py:

  - content_version() seeds to 1 and bump_content_version() increments it
  - a committed write to a content model trips the after_commit hook and bumps
    the version (so dependent cache keys become unreachable)
  - a rolled-back write does NOT bump the version
  - the dashboard stats read-through reflects new data immediately after a
    commit (no staleness), because the version embedded in its key changed
  - with the default NullCache backend, caching is a no-op (always fresh)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_cache -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-cache-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
# Use an in-process cache so we can exercise the real get/set/inc paths.
# (Single-process test only — SimpleCache is never used in production.)
os.environ['CACHE_TYPE'] = 'SimpleCache'


class _CacheTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        with cls.app.app_context():
            db.create_all()

    def setUp(self):
        from myapp.extensions import cache
        self.ctx = self.app.app_context()
        self.ctx.push()
        cache.clear()

    def tearDown(self):
        from myapp.extensions import db
        db.session.rollback()
        self.ctx.pop()


class TestContentVersion(_CacheTestBase):
    def test_seeds_to_one(self):
        from myapp.services.cache import content_version
        self.assertEqual(content_version(), 1)

    def test_bump_increments(self):
        from myapp.services.cache import bump_content_version, content_version
        start = content_version()
        bump_content_version()
        self.assertEqual(content_version(), start + 1)

    def test_commit_of_content_model_bumps_version(self):
        from myapp.database import Item
        from myapp.extensions import db
        from myapp.services.cache import content_version

        before = content_version()
        db.session.add(Item(name='Cache Test Item'))
        db.session.commit()
        self.assertEqual(content_version(), before + 1)

    def test_rollback_does_not_bump_version(self):
        from myapp.database import Item
        from myapp.extensions import db
        from myapp.services.cache import content_version

        before = content_version()
        db.session.add(Item(name='Rolled Back Item'))
        db.session.flush()   # trips after_flush, flags the session
        db.session.rollback()
        # commit something unrelated-free to ensure no deferred bump leaks
        self.assertEqual(content_version(), before)


class TestDashboardStatsCaching(_CacheTestBase):
    def _stats(self):
        from flask_login import AnonymousUserMixin
        from myapp.blueprints.dashboard import _get_stats
        return _get_stats(AnonymousUserMixin())

    def test_no_staleness_after_commit(self):
        from myapp.database import Item
        from myapp.extensions import db

        first = self._stats()
        # Prime the cache again — second read should be the cached value.
        self.assertEqual(self._stats(), first)

        # A new public item must be reflected immediately (version bump
        # invalidates the previously cached entry).
        db.session.add(Item(name='Freshly Added'))  # defaults: not private
        db.session.commit()

        after = self._stats()
        self.assertEqual(after['total_items'], first['total_items'] + 1)


class TestNullCacheIsNoOp(unittest.TestCase):
    """With no backend configured the cache disables itself (always fresh)."""

    def test_nullcache_default(self):
        # Build a separate app with caching unconfigured.
        saved = os.environ.pop('CACHE_TYPE', None)
        try:
            from myapp.app import create_app
            app = create_app()
            with app.app_context():
                from myapp.extensions import cache
                from myapp.services.cache import bump_content_version, content_version
                # NullCache backend selected when nothing is configured.
                self.assertIn('Null', type(cache.cache).__name__)
                # NullCache.get always misses; content_version still returns a
                # sane default and bump is a harmless no-op.
                cache.set('x', 1)
                self.assertIsNone(cache.get('x'))
                self.assertEqual(content_version(), 1)
                bump_content_version()  # must not raise
        finally:
            if saved is not None:
                os.environ['CACHE_TYPE'] = saved


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
