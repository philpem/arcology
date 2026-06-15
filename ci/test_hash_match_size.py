"""
Hash-match file-size semantics tests.

The hash database can hold KnownFile rows with no recorded size (NULL),
meaning "size unknown".  Size matching must be *lenient*: a NULL-size
KnownFile matches a file of any size, and a sized file only fails to match
a KnownFile whose recorded size differs.

These tests pin the three matching paths to the same lenient behaviour so
that upload-time linking (find_known_file) and the rescan path
(_find_known_files_batch / rescan_hashes_for_queryset) cannot drift apart:

  - find_known_file()              -> live upload-time linking (api.add_files)
  - _find_known_files_batch()      -> rescan helper
  - rescan_hashes_for_queryset()   -> end-to-end rescan over ExtractedFiles

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hash_match_size -v
"""

import hashlib
import itertools
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashsize-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

# Each test class shares one in-memory DB, and find_known_file() returns the
# earliest-inserted match for a given hash.  Give every test a distinct md5 so
# KnownFiles created by other tests can never satisfy the lookup under test.
_md5_counter = itertools.count(1)


def _unique_md5():
    return hashlib.md5(f'hashsize-{next(_md5_counter)}'.encode()).hexdigest()


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _db.create_all()
    return app, _db


class _HashMatchBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def _make_known_file(self, *, file_size, md5):
        """Create an active-database KnownFile with the given size and return its id."""
        from myapp.database import HashDatabase, KnownFile

        hdb = HashDatabase(name=f'DB-{md5[:8]}', version='1.0')
        self.db.session.add(hdb)
        self.db.session.flush()
        kf = KnownFile(database_id=hdb.id, filename='F.BIN', file_size=file_size, md5=md5)
        self.db.session.add(kf)
        self.db.session.commit()
        return kf.id


class TestFindKnownFileSize(_HashMatchBase):
    """find_known_file() — the live upload-time linking path."""

    def test_null_size_known_file_matches_any_size(self):
        from myapp.services.hash_rescan import find_known_file

        with self.app.app_context():
            md5 = _unique_md5()
            kf_id = self._make_known_file(file_size=None, md5=md5)
            # A file with a concrete size must still match a NULL-size KnownFile.
            found = find_known_file(md5=md5, file_size=4096)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, kf_id)

    def test_matching_size_matches(self):
        from myapp.services.hash_rescan import find_known_file

        with self.app.app_context():
            md5 = _unique_md5()
            kf_id = self._make_known_file(file_size=4096, md5=md5)
            found = find_known_file(md5=md5, file_size=4096)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, kf_id)

    def test_differing_size_does_not_match(self):
        from myapp.services.hash_rescan import find_known_file

        with self.app.app_context():
            md5 = _unique_md5()
            self._make_known_file(file_size=4096, md5=md5)
            # Sizes differ on both sides -> no match.
            found = find_known_file(md5=md5, file_size=512)
            self.assertIsNone(found)

    def test_no_size_supplied_matches_sized_known_file(self):
        from myapp.services.hash_rescan import find_known_file

        with self.app.app_context():
            md5 = _unique_md5()
            kf_id = self._make_known_file(file_size=4096, md5=md5)
            # Caller supplies no size -> size filter is not applied.
            found = find_known_file(md5=md5)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, kf_id)


class TestBatchMatchSizeAgreement(_HashMatchBase):
    """_find_known_files_batch() must agree with find_known_file() on size."""

    def _extracted_file(self, *, file_size, md5):
        """Build (unsaved) ExtractedFile-like stub with the attributes the matcher reads."""
        from myapp.database import ExtractedFile

        return ExtractedFile(
            partition_id=0, path='f.bin', filename='f.bin',
            file_size=file_size, md5=md5, is_directory=False,
        )

    def test_null_size_known_file_matches_any_size(self):
        from myapp.services.hash_rescan import _find_known_files_batch

        with self.app.app_context():
            md5 = _unique_md5()
            kf_id = self._make_known_file(file_size=None, md5=md5)
            ef = self._extracted_file(file_size=4096, md5=md5)
            ef.id = 1
            result = _find_known_files_batch([ef])
            self.assertIn(1, result)
            self.assertEqual(result[1].id, kf_id)

    def test_differing_size_does_not_match(self):
        from myapp.services.hash_rescan import _find_known_files_batch

        with self.app.app_context():
            md5 = _unique_md5()
            self._make_known_file(file_size=4096, md5=md5)
            ef = self._extracted_file(file_size=512, md5=md5)
            ef.id = 1
            result = _find_known_files_batch([ef])
            self.assertNotIn(1, result)


class TestFindKnownFilesForRecords(_HashMatchBase):
    """find_known_files_for_records() must agree with find_known_file().

    This is the batched matcher used by api.add_files() to register a whole
    partition listing with a single query instead of one query per file.
    """

    def test_returns_match_aligned_with_input_order(self):
        from myapp.services.hash_rescan import find_known_files_for_records

        with self.app.app_context():
            md5a = _unique_md5()
            md5b = _unique_md5()
            kf_a = self._make_known_file(file_size=4096, md5=md5a)
            kf_b = self._make_known_file(file_size=None, md5=md5b)
            records = [
                {'md5': md5a, 'sha1': None, 'file_size': 4096},   # exact size match
                {'md5': _unique_md5(), 'sha1': None, 'file_size': 1},  # no match
                {'md5': md5b, 'sha1': None, 'file_size': 9999},   # NULL-size match
                {'md5': None, 'sha1': None, 'file_size': None},   # no hash -> None
            ]
            result = find_known_files_for_records(records)
            self.assertEqual(len(result), len(records))
            self.assertEqual(result[0].id, kf_a)
            self.assertIsNone(result[1])
            self.assertEqual(result[2].id, kf_b)
            self.assertIsNone(result[3])

    def test_differing_size_does_not_match(self):
        from myapp.services.hash_rescan import find_known_files_for_records

        with self.app.app_context():
            md5 = _unique_md5()
            self._make_known_file(file_size=4096, md5=md5)
            result = find_known_files_for_records(
                [{'md5': md5, 'sha1': None, 'file_size': 512}]
            )
            self.assertIsNone(result[0])

    def test_empty_input_returns_empty_list(self):
        from myapp.services.hash_rescan import find_known_files_for_records

        with self.app.app_context():
            self.assertEqual(find_known_files_for_records([]), [])


class TestRescanLinksNullSize(_HashMatchBase):
    """End-to-end: a rescan links a sized file to a NULL-size KnownFile."""

    def test_rescan_links_sized_file_to_null_size_known_file(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
        )
        from myapp.services.hash_rescan import rescan_hashes_for_queryset

        with self.app.app_context():
            md5 = _unique_md5()
            kf_id = self._make_known_file(file_size=None, md5=md5)

            item = Item(name='HashSize Item')
            self.db.session.add(item)
            self.db.session.flush()
            artefact = Artefact(
                item_id=item.id, label='A', artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='a.img', storage_path='a.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            partition = Partition(
                artefact_id=artefact.id, partition_index=0,
                filesystem=FilesystemType.UNKNOWN,
            )
            self.db.session.add(partition)
            self.db.session.flush()
            ef = ExtractedFile(
                partition_id=partition.id, path='f.bin', filename='f.bin',
                file_size=4096, md5=md5, is_directory=False, is_known=False,
            )
            self.db.session.add(ef)
            self.db.session.commit()
            ef_id = ef.id

            updated, total = rescan_hashes_for_queryset(
                ExtractedFile.query.filter(ExtractedFile.partition_id == partition.id)
            )
            self.assertEqual(total, 1)
            self.assertEqual(updated, 1)

            refreshed = self.db.session.get(ExtractedFile, ef_id)
            self.assertTrue(refreshed.is_known)
            self.assertEqual(refreshed.known_file_id, kf_id)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
