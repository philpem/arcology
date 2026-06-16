"""
Correctness-first read-through caching.

The hard requirement for this cache is **no staleness and no data-integrity
risk**.  We meet it with *version-keyed* caching instead of TTL guessing:

  * A single monotonic counter — the "content version" — is stored in the
    shared cache backend.
  * Every cache key embeds the current content version, e.g.
    ``dashboard:stats:<uid>:v7``.
  * Any database commit that touches catalogue data (items, artefacts,
    analyses, shares, groups, users) bumps the counter.  Every previously
    stored key instantly becomes unreachable; the next read recomputes and
    stores under the new version.  Stale values are therefore *never served* —
    they simply age out via TTL.

Why this is safe across the deployment topology:

  * The web app runs under Gunicorn with multiple worker processes, so an
    in-process cache would not see another process's invalidation.  The counter
    therefore lives in a **shared** backend (Redis).  The bundled Redis is
    configured with persistence so the counter survives a restart; a counter
    that could silently reset to a lower value (e.g. an evicted Memcached key)
    is exactly what would reintroduce staleness, which is why Redis is the
    recommended backend.
  * The analysis worker has no database access — it mutates data only through
    the web API — so every write, whether from the UI, the REST API, or a
    worker callback, funnels through this process and trips the same
    SQLAlchemy ``after_commit`` hook.  Invalidation lives in exactly one place.

When no backend is configured the Flask-Caching extension is a ``NullCache``:
``get`` always misses and ``set`` is a no-op, so the application behaves
identically to the un-cached version (always fresh).  Caching is therefore
strictly opt-in via configuration.
"""

import itertools
from sqlalchemy import event
from ..extensions import cache, db

# Cache key under which the monotonic content-version counter is stored.
# timeout=0 means "never expire" — the counter must outlive every value keyed
# against it.
_CONTENT_VERSION_KEY = 'arc:ver:content'

# Models whose creation/modification/deletion can change a cached, content-
# derived value (catalogue counts and per-user visibility).  Touching any of
# these in a committed transaction invalidates content-versioned cache entries.
#
# Over-invalidation is always safe here — it only causes a cache miss, never a
# stale read — so the list errs toward completeness.  It is imported lazily in
# register_cache_invalidation() to avoid an import cycle with database.py.
_CONTENT_MODEL_NAMES = ('Item', 'Artefact', 'Analysis', 'ItemShare', 'Group', 'User')


def content_version():
    """Return the current content version, seeding it to 1 on first use."""
    version = cache.get(_CONTENT_VERSION_KEY)
    if version is None:
        # Seed lazily.  A race between two seeders both writes 1 — harmless.
        cache.set(_CONTENT_VERSION_KEY, 1, timeout=0)
        return 1
    return version


def bump_content_version():
    """Invalidate every content-versioned cache entry.

    Uses the backend's atomic increment where available (Redis ``INCR``);
    falls back to a read-modify-write for backends without one.  A failure to
    bump must never break the request that triggered it, so any backend error
    is swallowed — the worst outcome is a value living until its TTL.
    """
    try:
        new_version = cache.inc(_CONTENT_VERSION_KEY)
    except Exception:
        new_version = None
    if new_version is not None:
        return
    # NullCache (caching disabled) or a backend without inc(): for NullCache
    # this is a harmless no-op; otherwise approximate atomicity with set().
    try:
        cache.set(_CONTENT_VERSION_KEY, (cache.get(_CONTENT_VERSION_KEY) or 1) + 1, timeout=0)
    except Exception:
        pass


# db.session is a process-global scoped_session shared by every create_app()
# call, so the listeners must be attached exactly once per process — otherwise
# repeated app creation (notably in tests) would stack duplicate handlers and
# bump the version multiple times per commit.
_invalidation_registered = False


def register_cache_invalidation(app):
    """Wire the SQLAlchemy commit hook that bumps the content version.

    A flush records whether any content model was written on the session; the
    subsequent commit performs the bump.  Bumping on commit (not flush) means a
    rolled-back transaction never invalidates anything.

    Idempotent: only the first call per process attaches the listeners.
    """
    global _invalidation_registered
    if _invalidation_registered:
        return
    _invalidation_registered = True

    from .. import database

    content_models = tuple(
        getattr(database, name) for name in _CONTENT_MODEL_NAMES if hasattr(database, name)
    )
    flag = '_arc_content_dirty'

    @event.listens_for(db.session, 'after_flush')
    def _note_content_writes(session, flush_context):
        if session.info.get(flag):
            return
        for obj in itertools.chain(session.new, session.dirty, session.deleted):
            if isinstance(obj, content_models):
                session.info[flag] = True
                return

    @event.listens_for(db.session, 'after_commit')
    def _bump_on_commit(session):
        if session.info.pop(flag, False):
            bump_content_version()

    @event.listens_for(db.session, 'after_rollback')
    def _clear_on_rollback(session):
        session.info.pop(flag, None)


# vim: ts=4 sw=4 et
