"""
Tests for artefact SHA-1: the `compute_file_hashes_full` helper and
`flask backfill-artefact-sha1`.

Covers:
  - compute_file_hashes returns the (md5, sha256) dedup identity, and
    compute_file_hashes_full adds sha1 — both with correct digests.
  - The backfill command computes and stores SHA-1 for an artefact whose bytes
    live in the storage backend.
  - --dry-run touches no rows.
  - Artefacts that already have a SHA-1 are skipped (idempotent re-run).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_artefact_sha1_backfill -v
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

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-artefact-sha1-test-secret')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_CONTENT = b'hello sha1 world\n' * 64
_MD5 = hashlib.md5(_CONTENT).hexdigest()
_SHA1 = hashlib.sha1(_CONTENT).hexdigest()
_SHA256 = hashlib.sha256(_CONTENT).hexdigest()


class TestComputeFileHashesSha1(unittest.TestCase):
    """The web-side hash helpers: identity pair vs full digest set."""

    def test_compute_file_hashes_is_dedup_identity(self):
        from myapp.services.artefact_storage import compute_file_hashes
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(_CONTENT)
            path = tmp.name
        try:
            self.assertEqual(compute_file_hashes(path), (_MD5, _SHA256))
            self.assertEqual(
                compute_file_hashes(path, with_size=True),
                (_MD5, _SHA256, len(_CONTENT)),
            )
        finally:
            os.unlink(path)

    def test_compute_file_hashes_full_includes_sha1(self):
        from myapp.services.artefact_storage import compute_file_hashes_full
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(_CONTENT)
            path = tmp.name
        try:
            self.assertEqual(
                compute_file_hashes_full(path),
                (_MD5, _SHA1, _SHA256),
            )
            self.assertEqual(
                compute_file_hashes_full(path, with_size=True),
                (_MD5, _SHA1, _SHA256, len(_CONTENT)),
            )
        finally:
            os.unlink(path)


class TestBackfillArtefactSha1(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        from arcology_shared.storage import LocalStorage
        self._tmp = tempfile.TemporaryDirectory()
        uploads = Path(self._tmp.name) / 'uploads'
        outputs = Path(self._tmp.name) / 'outputs'
        uploads.mkdir()
        outputs.mkdir()
        self.app.storage = LocalStorage(uploads, outputs)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        # The command commits, so purge the rows it touched before the next test.
        from myapp.database import Artefact, Item
        self.db.session.rollback()
        Artefact.query.delete()
        Item.query.delete()
        self.db.session.commit()
        self.ctx.pop()
        self._tmp.cleanup()

    def _make_artefact(self, storage_path='sha1_test.img', sha1=None):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        item = Item(name='SHA1 Backfill Item')
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id,
            label='SHA1 Backfill Artefact',
            artefact_type=ArtefactType.RAW_SECTOR,
            original_filename=storage_path,
            storage_path=storage_path,
            storage_directory=StorageDirectory.UPLOADS,
            file_size=len(_CONTENT),
            md5=_MD5,
            sha256=_SHA256,
            sha1=sha1,
        )
        self.db.session.add(art)
        self.db.session.commit()
        # Place the artefact's bytes in the storage backend.
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(_CONTENT)
            local = tmp.name
        try:
            self.app.storage.put(
                self.app.storage.storage_key('uploads', storage_path), local)
        finally:
            os.unlink(local)
        return art

    def _run(self, *args):
        from click.testing import CliRunner
        from myapp.cli.backfill_artefact_sha1 import backfill_artefact_sha1
        with self.app.app_context():
            return CliRunner().invoke(backfill_artefact_sha1, list(args),
                                      catch_exceptions=False)

    def test_backfill_populates_sha1(self):
        art = self._make_artefact()
        self.assertIsNone(art.sha1)

        self._run()

        self.db.session.refresh(art)
        self.assertEqual(art.sha1, _SHA1)

    def test_dry_run_leaves_sha1_null(self):
        art = self._make_artefact()
        result = self._run('--dry-run')

        self.db.session.refresh(art)
        self.assertIsNone(art.sha1)
        self.assertIn('[dry-run]', result.output)

    def test_existing_sha1_is_skipped(self):
        art = self._make_artefact(sha1='f' * 40)
        self._run()

        self.db.session.refresh(art)
        # Untouched — the query only selects rows with a NULL sha1.
        self.assertEqual(art.sha1, 'f' * 40)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
