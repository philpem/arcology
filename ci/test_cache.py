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
        # Discard the session so its .info (incl. the dirty flag) does not leak
        # into the next test — mirrors Flask-SQLAlchemy's per-request remove().
        db.session.remove()
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
        # No commit happened, so the version must not have moved.
        self.assertEqual(content_version(), before)

    def test_bulk_dml_bumps_version(self):
        """Bulk/core DML on a content model must invalidate (do_orm_execute)."""
        from sqlalchemy import update
        from myapp.database import Item
        from myapp.extensions import db
        from myapp.services.cache import content_version

        db.session.add(Item(name='Bulk Target'))
        db.session.commit()

        before = content_version()
        # Core/bulk UPDATE — never appears in session.dirty, so after_flush
        # alone would miss it; do_orm_execute must catch it.
        db.session.execute(update(Item).values(description='bulk-updated'))
        db.session.commit()
        self.assertEqual(content_version(), before + 1)

        before = content_version()
        Item.query.update({'name': 'renamed'})  # legacy bulk Query.update
        db.session.commit()
        self.assertEqual(content_version(), before + 1)

    def test_savepoint_rollback_still_bumps(self):
        """A committed content change survives a nested-transaction rollback.

        Regression for the bug where after_rollback (which fires on SAVEPOINT
        rollback too) cleared the dirty flag, so the outer commit failed to
        invalidate — serving stale data.
        """
        from myapp.database import Item
        from myapp.extensions import db
        from myapp.services.cache import content_version

        before = content_version()
        item = Item(name='Outer Write')
        db.session.add(item)
        db.session.flush()           # flags the session (content write)
        try:
            with db.session.begin_nested():   # SAVEPOINT
                db.session.add(Item(name='Inner Doomed'))
                db.session.flush()
                raise RuntimeError('boom')    # roll the savepoint back
        except RuntimeError:
            pass
        db.session.commit()          # outer write commits — must bump
        self.assertEqual(content_version(), before + 1)


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


class TestCachePerId(_CacheTestBase):
    def test_reads_through_and_caches(self):
        from myapp.services.cache import cache_per_id

        calls = []

        def compute(ids):
            calls.append(list(ids))
            return {i: i * 10 for i in ids}

        first = cache_per_id('t:x', [1, 2, 3], compute)
        self.assertEqual(first, {1: 10, 2: 20, 3: 30})
        # Second call is fully served from cache — compute not invoked again.
        second = cache_per_id('t:x', [1, 2, 3], compute)
        self.assertEqual(second, {1: 10, 2: 20, 3: 30})
        self.assertEqual(len(calls), 1)

    def test_only_missing_ids_recomputed(self):
        from myapp.services.cache import cache_per_id

        calls = []

        def compute(ids):
            calls.append(sorted(ids))
            return {i: i * 10 for i in ids}

        cache_per_id('t:y', [1, 2], compute)
        cache_per_id('t:y', [2, 3, 4], compute)
        self.assertEqual(calls, [[1, 2], [3, 4]])  # 2 was already cached

    def test_empty_result_is_negatively_cached(self):
        from myapp.services.cache import cache_per_id

        calls = []

        def compute(ids):
            calls.append(list(ids))
            return {}  # nothing has a value

        self.assertEqual(cache_per_id('t:z', [1, 2], compute), {})
        self.assertEqual(cache_per_id('t:z', [1, 2], compute), {})
        # Known-empty ids must NOT be recomputed on the second call.
        self.assertEqual(len(calls), 1)

    def test_version_bump_invalidates(self):
        from myapp.services.cache import bump_content_version, cache_per_id

        def compute_a(ids):
            return {i: 'a' for i in ids}

        def compute_b(ids):
            return {i: 'b' for i in ids}

        self.assertEqual(cache_per_id('t:v', [1], compute_a), {1: 'a'})
        bump_content_version()
        # New version → previous key unreachable → recompute returns fresh value.
        self.assertEqual(cache_per_id('t:v', [1], compute_b), {1: 'b'})

    def test_per_user_prefixes_are_isolated(self):
        from myapp.services.cache import cache_per_id

        cache_per_id('t:u:alice', [1], lambda ids: {i: 'alice' for i in ids})
        # A different viewer prefix must not see alice's cached value.
        got = cache_per_id('t:u:bob', [1], lambda ids: {i: 'bob' for i in ids})
        self.assertEqual(got, {1: 'bob'})


class TestCacheValue(_CacheTestBase):
    def test_reads_through_then_caches(self):
        from myapp.services.cache import cache_value

        calls = []

        def compute():
            calls.append(1)
            return 42

        self.assertEqual(cache_value('t:val', 'sig', compute), 42)
        self.assertEqual(cache_value('t:val', 'sig', compute), 42)
        self.assertEqual(len(calls), 1)  # second call served from cache

    def test_zero_is_cached_not_treated_as_miss(self):
        """A genuine 0 (e.g. a search count of zero) must cache, not recompute."""
        from myapp.services.cache import cache_value

        calls = []

        def compute():
            calls.append(1)
            return 0

        self.assertEqual(cache_value('t:zero', 'sig', compute), 0)
        self.assertEqual(cache_value('t:zero', 'sig', compute), 0)
        self.assertEqual(len(calls), 1)

    def test_distinct_signatures_isolated(self):
        from myapp.services.cache import cache_value

        a = cache_value('t:sig', 'query-a', lambda: 'A')
        b = cache_value('t:sig', 'query-b', lambda: 'B')
        self.assertEqual((a, b), ('A', 'B'))

    def test_version_bump_invalidates(self):
        from myapp.services.cache import bump_content_version, cache_value

        self.assertEqual(cache_value('t:v', 'sig', lambda: 'old'), 'old')
        bump_content_version()
        self.assertEqual(cache_value('t:v', 'sig', lambda: 'new'), 'new')

    def test_per_user_isolation(self):
        from flask_login import AnonymousUserMixin
        from myapp.services.cache import cache_value

        class _User:
            is_authenticated = True

            def __init__(self, uid):
                self._uid = uid

            def get_id(self):
                return self._uid

        anon = cache_value('t:u', 'sig', lambda: 'anon', user=AnonymousUserMixin())
        alice = cache_value('t:u', 'sig', lambda: 'alice', user=_User('alice'))
        # Different viewers must not share the entry.
        self.assertEqual((anon, alice), ('anon', 'alice'))


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
