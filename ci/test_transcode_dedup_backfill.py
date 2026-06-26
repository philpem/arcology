"""Backfill + redo of content-addressed transcode outputs (no reanalysis).

Covers the operator commands in ``myapp.services.transcode_dedup``:

  * dedup_transcode_outputs -- collapse legacy duplicate transcodes onto a
    shared OutputBlob using the already-recorded SOURCE hash (no re-encode);
  * invalidate_transcodes / requeue_targets -- discard a bad transcode and
    re-queue a fresh encode.

Run:
    python -m unittest ci.test_transcode_dedup_backfill -v
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')


class TestTranscodeDedupBackfill(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls._db = _db
        cls._tmp = tempfile.TemporaryDirectory()
        uploads = Path(cls._tmp.name) / 'uploads'
        outputs = Path(cls._tmp.name) / 'outputs'
        uploads.mkdir()
        outputs.mkdir()
        cls.app.storage = LocalStorage(uploads, outputs)
        with cls.app.app_context():
            _db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # -- builders ------------------------------------------------------------

    def _write_output(self, rel_path, data):
        storage = self.app.storage
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            storage.put(storage.storage_key('outputs', rel_path), Path(tmp))
        finally:
            os.unlink(tmp)

    def _replay_source(self, label, source_sha, file_path='Movies/Clip,ae7'):
        """Item + disc artefact + partition + extracted source + ReplayMovie."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
            ReplayMovie,
            StorageDirectory,
        )
        item = Item(name=label)
        self._db.session.add(item)
        self._db.session.flush()
        art = Artefact(
            item_id=item.id, label=label, artefact_type=ArtefactType.HFE,
            original_filename=f'{label}.hfe', storage_path=f'{label}.hfe',
            storage_directory=StorageDirectory.UPLOADS,
            md5='f' * 32, sha256='f' * 64,
        )
        self._db.session.add(art)
        self._db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0,
                         filesystem=FilesystemType.UNKNOWN)
        self._db.session.add(part)
        self._db.session.flush()
        self._db.session.add(ExtractedFile(
            partition_id=part.id, path=file_path, filename='Clip',
            sha256=source_sha))
        movie = ReplayMovie(artefact_id=art.id, file_path=file_path)
        self._db.session.add(movie)
        self._db.session.flush()
        return art, movie

    # -- tests ---------------------------------------------------------------

    def test_legacy_output_migrates_to_canonical_blob(self):
        from myapp.database import OutputBlob
        from myapp.services.transcode_dedup import dedup_transcode_outputs
        source_sha = 'a' * 64
        data = b'fake-mp4-bytes'
        legacy = 'legacy/itemA/clip.mp4'
        canonical = f'media/{source_sha}/1/movie.mp4'
        with self.app.app_context():
            try:
                _art, movie = self._replay_source('LegacyOne', source_sha)
                movie.mp4_output_path = legacy
                self._db.session.flush()
                self._write_output(legacy, data)

                stats = dedup_transcode_outputs()
                self.assertEqual(stats.linked, 1)
                self.assertEqual(stats.blobs_created, 1)
                self.assertEqual(stats.skipped, 0)

                blob = OutputBlob.query.filter_by(storage_path=canonical).one()
                # Output hash recorded is the real transcoded-file hash.
                self.assertEqual(blob.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(blob.file_size, len(data))

                self._db.session.refresh(movie)
                self.assertEqual(movie.mp4_output_blob_id, blob.id)
                self.assertEqual(movie.mp4_output_path, canonical)

                storage = self.app.storage
                self.assertTrue(storage.exists(
                    storage.storage_key('outputs', canonical)))
                self.assertFalse(storage.exists(
                    storage.storage_key('outputs', legacy)))
            finally:
                self._db.session.rollback()

    def test_two_sources_share_one_blob_and_reclaim_duplicate(self):
        from myapp.database import OutputBlob
        from myapp.services.transcode_dedup import dedup_transcode_outputs
        source_sha = 'b' * 64
        data = b'shared-output'
        canonical = f'media/{source_sha}/1/movie.mp4'
        with self.app.app_context():
            try:
                _a1, m1 = self._replay_source('DupA', source_sha)
                _a2, m2 = self._replay_source('DupB', source_sha)
                m1.mp4_output_path = 'legacy/A/clip.mp4'
                m2.mp4_output_path = 'legacy/B/clip.mp4'
                self._db.session.flush()
                self._write_output('legacy/A/clip.mp4', data)
                self._write_output('legacy/B/clip.mp4', data)

                stats = dedup_transcode_outputs()
                self.assertEqual(stats.linked, 2)
                self.assertEqual(stats.blobs_created, 1)
                self.assertEqual(stats.files_reclaimed, 1)

                blobs = OutputBlob.query.filter_by(storage_path=canonical).all()
                self.assertEqual(len(blobs), 1)
                self._db.session.refresh(m1)
                self._db.session.refresh(m2)
                self.assertEqual(m1.mp4_output_blob_id, blobs[0].id)
                self.assertEqual(m2.mp4_output_blob_id, blobs[0].id)

                storage = self.app.storage
                self.assertFalse(storage.exists(
                    storage.storage_key('outputs', 'legacy/B/clip.mp4')))
            finally:
                self._db.session.rollback()

    def test_unresolved_source_is_skipped(self):
        from myapp.services.transcode_dedup import dedup_transcode_outputs
        with self.app.app_context():
            try:
                # ReplayMovie whose file_path matches no ExtractedFile hash.
                _art, movie = self._replay_source('NoSource', 'c' * 64)
                movie.file_path = 'Movies/Missing,ae7'
                movie.mp4_output_path = 'legacy/none/clip.mp4'
                self._db.session.flush()
                self._write_output('legacy/none/clip.mp4', b'x')

                stats = dedup_transcode_outputs()
                self.assertEqual(stats.linked, 0)
                self.assertGreaterEqual(stats.skipped, 1)
                self._db.session.refresh(movie)
                self.assertIsNone(movie.mp4_output_blob_id)
            finally:
                self._db.session.rollback()

    def test_redo_invalidates_and_targets_reanalysis(self):
        from arcology_shared.enums import AnalysisType
        from myapp.database import OutputBlob
        from myapp.services.transcode_dedup import (
            dedup_transcode_outputs,
            invalidate_transcodes,
            requeue_targets,
        )
        source_sha = 'd' * 64
        canonical = f'media/{source_sha}/1/movie.mp4'
        with self.app.app_context():
            try:
                art, movie = self._replay_source('RedoOne', source_sha)
                movie.mp4_output_path = 'legacy/redo/clip.mp4'
                self._db.session.flush()
                self._write_output('legacy/redo/clip.mp4', b'bad-output')
                dedup_transcode_outputs()
                self.assertTrue(
                    OutputBlob.query.filter_by(storage_path=canonical).count())

                # Resolve re-queue targets BEFORE invalidation clears the rows.
                targets = requeue_targets({source_sha})
                self.assertIn((art.id, AnalysisType.REPLAY_PROCESS), targets)

                counts = invalidate_transcodes({source_sha})
                self.assertEqual(counts['blobs'], 1)
                self.assertGreaterEqual(counts['rows'], 1)

                self.assertEqual(
                    OutputBlob.query.filter_by(storage_path=canonical).count(), 0)
                self._db.session.refresh(movie)
                self.assertIsNone(movie.mp4_output_blob_id)
                self.assertIsNone(movie.mp4_output_path)
                storage = self.app.storage
                self.assertFalse(storage.exists(
                    storage.storage_key('outputs', canonical)))
            finally:
                self._db.session.rollback()

    def test_dedup_is_idempotent(self):
        from myapp.services.transcode_dedup import dedup_transcode_outputs
        source_sha = 'e' * 64
        with self.app.app_context():
            try:
                _art, movie = self._replay_source('Idem', source_sha)
                movie.mp4_output_path = 'legacy/idem/clip.mp4'
                self._db.session.flush()
                self._write_output('legacy/idem/clip.mp4', b'data')
                dedup_transcode_outputs()
                # Second pass: the row is already linked, nothing to do.
                stats = dedup_transcode_outputs()
                self.assertEqual(stats.linked, 0)
                self.assertEqual(stats.blobs_created, 0)
                self.assertEqual(stats.files_reclaimed, 0)
            finally:
                self._db.session.rollback()


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
