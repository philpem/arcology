"""
Content-keyed media transcode deduplication.

When two artefacts hold the byte-identical source media (e.g. the same Acorn
Replay demo or the same AVI), the expensive transcode runs once and the output
is shared:

  * worker  — transcode_cached skips ffmpeg on a cache hit (no second encode);
  * web     — both ReplayMovie/MediaFile rows link the one shared OutputBlob;
  * GC      — the shared output is reclaimed only when the LAST referencing
              artefact is deleted (refcount-aware), never on the first;
  * serving — a content-addressed output is gated against all owning artefacts.

Run:
    python -m unittest ci.test_transcode_dedup -v
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')


# ─────────────────────────────────────────────────────────────────────────────
# Worker: transcode_cached skips the encode on a cache hit
# ─────────────────────────────────────────────────────────────────────────────

class TestTranscodeCachedSkip(unittest.TestCase):

    def _worker(self):
        w = MagicMock()
        w.save_output_file.side_effect = lambda p, name, subdir=None: f'{subdir}/{name}'
        return w

    def test_miss_produces_and_reports_output_hashes(self):
        from worker.arcworker.analyses import _common
        w = self._worker()
        w.api.get_transcode_cache.return_value = None  # miss

        produced = {'n': 0}

        def _produce():
            produced['n'] += 1
            return Path('/work/out.mp4'), None

        with patch.object(_common, 'compute_file_hash',
                          side_effect=[('m', 'a' * 64, 4), ('m2', 'b' * 64, 9)]):
            res = _common.transcode_cached(
                w, input_path=Path('/work/in.avi'), output_ext='mp4',
                produce=_produce)

        self.assertEqual(produced['n'], 1)            # encoded once
        self.assertFalse(res['cache_hit'])
        self.assertEqual(res['input_sha256'], 'a' * 64)
        # The produced output's own hash/size is reported for blob registration.
        self.assertEqual(res['mp4_sha256'], 'b' * 64)
        self.assertEqual(res['mp4_file_size'], 9)
        # Stored content-addressed (keyed on the SOURCE hash), not per-artefact.
        self.assertEqual(res['mp4_output_path'], f'media/{"a" * 64}/1/movie.mp4')

    def test_hit_skips_encode(self):
        from worker.arcworker.analyses import _common
        w = self._worker()
        w.api.get_transcode_cache.return_value = {
            'mp4_output_path': f'media/{"a" * 64}/1/movie.mp4',
            'poster_path': f'media/{"a" * 64}/1/poster.jpg',
        }

        def _produce():
            raise AssertionError('produce() must not run on a cache hit')

        with patch.object(_common, 'compute_file_hash',
                          return_value=('m', 'a' * 64, 4)):
            res = _common.transcode_cached(
                w, input_path=Path('/work/in.avi'), output_ext='mp4',
                produce=_produce)

        self.assertTrue(res['cache_hit'])
        self.assertEqual(res['mp4_output_path'], f'media/{"a" * 64}/1/movie.mp4')
        self.assertEqual(res['poster_path'], f'media/{"a" * 64}/1/poster.jpg')
        w.save_output_file.assert_not_called()        # nothing re-stored


# ─────────────────────────────────────────────────────────────────────────────
# Web: shared OutputBlob + refcount-aware GC + serving gate
# ─────────────────────────────────────────────────────────────────────────────

_SHA = 'd' * 64
_SUBDIR = f'media/{_SHA}/1'
_MP4 = f'{_SUBDIR}/movie.mp4'


class TestDedupDbBacked(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls._db = _db
        with cls.app.app_context():
            _db.create_all()

    def _artefact(self, item, label):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, StorageDirectory
        art = Artefact(
            item_id=item.id, label=label,
            artefact_type=ArtefactType.HFE,
            original_filename=f'{label}.hfe', storage_path=f'{label}.hfe',
            storage_directory=StorageDirectory.UPLOADS,
            md5='f' * 32, sha256='f' * 64,
        )
        self._db.session.add(art)
        self._db.session.flush()
        return art

    def _media_entry(self, *, cache_hit):
        """A MEDIA_TRANSCODE result entry for the same content-addressed output.

        The first (miss) carries the produced output's hash/size; a later hit
        reuses the path and omits them (the web side links the existing blob by
        its unique storage path).
        """
        entry = {
            'file_path': 'Movies/Clip.avi',
            'media_kind': 'video',
            'mp4_output_path': _MP4,
            'poster_path': None,
            'input_sha256': _SHA,
        }
        if not cache_hit:
            entry['mp4_file_size'] = 123
            entry['mp4_sha256'] = 'e' * 64
        return entry

    def _ingest(self, artefact, *, cache_hit):
        from myapp.services.search_index import handle_media_transcode
        analysis = types.SimpleNamespace(artefact_id=artefact.id)
        handle_media_transcode(analysis, {'transcoded': [self._media_entry(cache_hit=cache_hit)]})
        self._db.session.flush()

    def test_identical_source_shares_one_output_blob(self):
        from myapp.database import Item, MediaFile, OutputBlob
        with self.app.app_context():
            try:
                item = Item(name='Dedup')
                self._db.session.add(item)
                self._db.session.flush()
                a1 = self._artefact(item, 'discA')
                a2 = self._artefact(item, 'discB')

                self._ingest(a1, cache_hit=False)   # first: encodes, creates blob
                self._ingest(a2, cache_hit=True)     # second: reuses the blob

                blobs = OutputBlob.query.filter_by(storage_path=_MP4).all()
                self.assertEqual(len(blobs), 1, 'one shared OutputBlob expected')
                blob = blobs[0]

                rows = MediaFile.query.filter(
                    MediaFile.artefact_id.in_([a1.id, a2.id])).all()
                self.assertEqual(len(rows), 2)
                for r in rows:
                    self.assertEqual(r.mp4_output_blob_id, blob.id)
            finally:
                self._db.session.rollback()

    def test_gc_refcount_keeps_blob_until_last_owner_deleted(self):
        from myapp.database import Item, OutputBlob
        from myapp.services.artefact_lifecycle import _collect_item_cleanup_keys
        with self.app.app_context():
            try:
                item = Item(name='DedupGC')
                self._db.session.add(item)
                self._db.session.flush()
                a1 = self._artefact(item, 'gcA')
                a2 = self._artefact(item, 'gcB')
                self._ingest(a1, cache_hit=False)
                self._ingest(a2, cache_hit=True)
                blob = OutputBlob.query.filter_by(storage_path=_MP4).one()

                # Deleting only artefact 1 must NOT orphan the shared blob:
                # artefact 2 still references those bytes.
                keys = _collect_item_cleanup_keys([a1.id])
                self.assertNotIn(blob.id, keys['output_blob_ids'])
                self.assertNotIn(f'outputs/{_MP4}', keys[_artefact_keys_hint()])

                # Deleting both (the last owners) DOES orphan it.
                keys = _collect_item_cleanup_keys([a1.id, a2.id])
                self.assertIn(blob.id, keys['output_blob_ids'])
                self.assertIn(f'outputs/{_MP4}', keys[_artefact_keys_hint()])
            finally:
                self._db.session.rollback()

    def test_serving_gate_resolves_all_owners(self):
        from myapp.database import Item
        from myapp.services.downloads import (
            output_access_decision,
            resolve_output_artefacts,
        )
        with self.app.app_context():
            try:
                item = Item(name='DedupServe')
                self._db.session.add(item)
                self._db.session.flush()
                a1 = self._artefact(item, 'svA')
                a2 = self._artefact(item, 'svB')
                self._ingest(a1, cache_hit=False)
                self._ingest(a2, cache_hit=True)

                owners = {a.id for a in resolve_output_artefacts(_MP4)}
                self.assertEqual(owners, {a1.id, a2.id})

                # sees_all caller (worker/admin), no restrictions -> serve.
                self.assertEqual(
                    output_access_decision(_MP4, None, sees_all=True), 'ok')
                # Unknown content-addressed path -> indistinguishable from absent.
                self.assertEqual(
                    output_access_decision('media/0000/1/movie.mp4', None, sees_all=True),
                    'not_found')
            finally:
                self._db.session.rollback()

    def test_serving_gate_restriction_needs_one_clean_owner(self):
        from myapp.database import ArtefactRestriction, Item, RestrictionType
        from myapp.services.downloads import output_access_decision
        with self.app.app_context():
            try:
                item = Item(name='DedupRestrict')
                self._db.session.add(item)
                self._db.session.flush()
                a1 = self._artefact(item, 'rxA')
                self._ingest(a1, cache_hit=False)
                # Restrict the only owner: the worker key never bypasses -> 403.
                self._db.session.add(ArtefactRestriction(
                    artefact_id=a1.id, restriction_type=RestrictionType.MALWARE))
                self._db.session.flush()
                self.assertEqual(
                    output_access_decision(_MP4, None, sees_all=True), 'restricted')

                # A second, unrestricted owner of the identical bytes reopens a
                # legitimate route -> serve.
                a2 = self._artefact(item, 'rxB')
                self._ingest(a2, cache_hit=True)
                self.assertEqual(
                    output_access_decision(_MP4, None, sees_all=True), 'ok')
            finally:
                self._db.session.rollback()


class TestLinkTranscodeBlobsCanonicalPath(unittest.TestCase):
    """_link_transcode_blobs keeps the row's stored path == the blob's path.

    get_or_create_blob deduplicates by (file_size, sha256), so a byte-identical
    output produced for a DIFFERENT source (two sound-only clips embedding the
    same title-card poster sprite) reuses the existing blob — whose storage_path
    was written for the first source.  The handler must store that canonical path
    so owner-resolution and refcount GC stay consistent with the blob.
    """

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls._db = _db
        with cls.app.app_context():
            _db.create_all()

    def test_collision_returns_canonical_blob_path(self):
        from myapp.database import StorageDirectory
        from myapp.services.search_index import _link_transcode_blobs
        from myapp.utils.blobs import get_or_create_blob
        with self.app.app_context():
            try:
                # An existing poster blob written for source X.
                x_path = f'media/{"1" * 64}/1/poster.png'
                blob, created = get_or_create_blob(
                    StorageDirectory.OUTPUTS, x_path, 10, 'p' * 64)
                self._db.session.flush()
                self.assertTrue(created)

                # Source Y produces a byte-identical poster, written to its own
                # content-addressed path, but the same (size, sha) -> same blob.
                y_path = f'media/{"2" * 64}/1/poster.png'
                mp4_id, poster_id, mp4_path, poster_path = _link_transcode_blobs({
                    'input_sha256': '2' * 64,
                    'mp4_output_path': f'media/{"2" * 64}/1/movie.m4a',
                    'mp4_file_size': 99, 'mp4_sha256': 'm' * 64,
                    'poster_path': y_path,
                    'poster_file_size': 10, 'poster_sha256': 'p' * 64,
                })
                # The poster links the EXISTING blob and reports its canonical
                # path (X's), not the path Y wrote.
                self.assertEqual(poster_id, blob.id)
                self.assertEqual(poster_path, x_path)
                # The mp4 (no collision) keeps its own freshly-created path.
                self.assertEqual(mp4_path, f'media/{"2" * 64}/1/movie.m4a')
                self.assertIsNotNone(mp4_id)
            finally:
                self._db.session.rollback()

    def test_legacy_entry_without_input_sha_is_unlinked(self):
        from myapp.services.search_index import _link_transcode_blobs
        with self.app.app_context():
            mp4_id, poster_id, mp4_path, poster_path = _link_transcode_blobs({
                'mp4_output_path': 'item/art/u.mp4', 'poster_path': None,
            })
            self.assertEqual((mp4_id, poster_id), (None, None))
            self.assertEqual(mp4_path, 'item/art/u.mp4')
            self.assertIsNone(poster_path)


def _artefact_keys_hint():
    from arcology_shared.hints import HintKey
    return HintKey.ARTEFACT_KEYS


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
