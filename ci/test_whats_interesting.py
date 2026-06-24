"""
Tests for the "What's interesting about this disc?" triage summary
(myapp/services/whats_interesting.py).

Covers:
  - the three buckets (standard OS / recognised software / unknown) and their
    file-count and byte totals;
  - base-OS database names and recognised product titles surfaced for display;
  - aggregation across an artefact's derived-artefact tree;
  - the empty / no-references edge cases.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_whats_interesting -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-interesting-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _h(seed: str) -> str:
    return (seed * 64)[:64]


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['LOGIN_DISABLED'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        from myapp.database import (
            Artefact,
            ExtractedFile,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            Platform,
            RecognisedProduct,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        for model in (RecognisedProduct, ExtractedFile, KnownFile, KnownProduct,
                      HashDatabase, Partition, Artefact, Item, Platform):
            model.query.delete()
        self.db.session.commit()
        plat = Platform(name='Acorn')
        self.db.session.add(plat)
        self.db.session.flush()
        self.item = Item(name='Discs', platform_id=plat.id)
        self.db.session.add(self.item)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    # -- fixture helpers ---------------------------------------------------

    def _artefact(self, label, *, parent=None):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, FilesystemType, Partition

        art = Artefact(
            item_id=self.item.id,
            label=label,
            artefact_type=ArtefactType.RAW_SECTOR,
            original_filename=f'{label}.img',
            storage_path=f'uploads/{label}.img',
            parent_artefact_id=parent.id if parent else None,
        )
        self.db.session.add(art)
        self.db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0,
                         filesystem=FilesystemType.ADFS)
        self.db.session.add(part)
        self.db.session.flush()
        art._part = part
        return art

    def _db_with_files(self, name, *, exclude, product_title, file_seeds):
        """Create a HashDatabase + product + known files; return the known files."""
        from myapp.database import HashDatabase, KnownFile, KnownProduct

        hdb = HashDatabase(name=name, exclude_from_similarity=exclude)
        self.db.session.add(hdb)
        self.db.session.flush()
        prod = KnownProduct(database_id=hdb.id, title=product_title)
        self.db.session.add(prod)
        self.db.session.flush()
        kfs = []
        for seed in file_seeds:
            kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                           filename=seed, sha256=_h(seed))
            self.db.session.add(kf)
            kfs.append(kf)
        self.db.session.flush()
        return hdb, prod, kfs

    def _file(self, art, path, *, size, known_file=None):
        from myapp.database import ExtractedFile

        ef = ExtractedFile(
            partition_id=art._part.id,
            path=path,
            filename=path.split('/')[-1],
            file_size=size,
            is_directory=False,
            sha256=_h(path),
            known_file_id=known_file.id if known_file else None,
        )
        self.db.session.add(ef)
        return ef

    def _summarise(self, *artefacts):
        from myapp.services.whats_interesting import summarise_artefact
        ids = [a.id for a in artefacts]
        return summarise_artefact(ids)


class TestBuckets(_Base):

    def test_three_buckets_and_byte_totals(self):
        _, _, os_kfs = self._db_with_files(
            'RISC OS 3.6', exclude=True, product_title='RISC OS',
            file_seeds=['osa', 'osb'])
        _, _, app_kfs = self._db_with_files(
            'Arcarc', exclude=False, product_title='!ArtWorks',
            file_seeds=['aw1'])

        art = self._artefact('disc')
        # Standard OS: 2 files, 100 + 200 bytes
        self._file(art, '!Boot.OSa', size=100, known_file=os_kfs[0])
        self._file(art, '!Boot.OSb', size=200, known_file=os_kfs[1])
        # Recognised software: 1 file, 4000 bytes
        self._file(art, 'Apps.!ArtWorks.foo', size=4000, known_file=app_kfs[0])
        # Unknown: 2 files, 50 + 70 bytes
        self._file(art, 'User.letter', size=50)
        self._file(art, 'User.notes', size=70)
        self.db.session.commit()

        s = self._summarise(art)
        self.assertEqual(s.standard_os.count, 2)
        self.assertEqual(s.standard_os.size, 300)
        self.assertEqual(s.recognised.count, 1)
        self.assertEqual(s.recognised.size, 4000)
        self.assertEqual(s.unknown.count, 2)
        self.assertEqual(s.unknown.size, 120)
        self.assertEqual(s.total_count, 5)
        self.assertEqual(s.total_bytes, 4420)
        self.assertTrue(s.has_files)
        self.assertTrue(s.has_references)

    def test_names_and_products_surfaced(self):
        _, _, os_kfs = self._db_with_files(
            'RISC OS 3.6', exclude=True, product_title='RISC OS',
            file_seeds=['osa'])
        _, _, app_kfs = self._db_with_files(
            'Arcarc', exclude=False, product_title='!ArtWorks 1.5',
            file_seeds=['aw1'])

        art = self._artefact('disc')
        self._file(art, '!Boot.OSa', size=100, known_file=os_kfs[0])
        self._file(art, 'Apps.!ArtWorks.foo', size=4000, known_file=app_kfs[0])
        self.db.session.commit()

        s = self._summarise(art)
        self.assertEqual(s.standard_os_names, ['RISC OS 3.6'])
        self.assertEqual(s.recognised_products, ['!ArtWorks 1.5'])

    def test_directories_excluded(self):
        art = self._artefact('disc')
        from myapp.database import ExtractedFile
        self.db.session.add(ExtractedFile(
            partition_id=art._part.id, path='Dir', filename='Dir',
            is_directory=True, file_size=0))
        self._file(art, 'Dir.file', size=10)
        self.db.session.commit()

        s = self._summarise(art)
        self.assertEqual(s.unknown.count, 1)
        self.assertEqual(s.total_count, 1)

    def test_recognised_product_from_recognition(self):
        """A folder-level RecognisedProduct surfaces even without a known_file link."""
        from myapp.database import RecognisedProduct
        _, prod, _ = self._db_with_files(
            'Arcarc', exclude=False, product_title='!Draw',
            file_seeds=['dw1'])
        art = self._artefact('disc')
        self._file(art, 'User.letter', size=50)
        self.db.session.add(RecognisedProduct(
            partition_id=art._part.id, product_id=prod.id, folder_path='Apps.!Draw'))
        self.db.session.commit()

        s = self._summarise(art)
        self.assertIn('!Draw', s.recognised_products)

    def test_aggregates_over_derived_tree(self):
        _, _, os_kfs = self._db_with_files(
            'RISC OS 3.6', exclude=True, product_title='RISC OS',
            file_seeds=['osa'])
        root = self._artefact('root')
        derived = self._artefact('derived', parent=root)
        self._file(root, '!Boot.OSa', size=100, known_file=os_kfs[0])
        self._file(derived, 'User.letter', size=50)
        self.db.session.commit()

        s = self._summarise(root, derived)
        self.assertEqual(s.standard_os.count, 1)
        self.assertEqual(s.unknown.count, 1)

    def test_empty_artefact(self):
        art = self._artefact('blank')
        self.db.session.commit()
        s = self._summarise(art)
        self.assertFalse(s.has_files)
        self.assertEqual(s.total_count, 0)
        self.assertEqual(s.standard_os_names, [])
        self.assertEqual(s.recognised_products, [])

    def test_all_unknown_has_no_references(self):
        art = self._artefact('disc')
        self._file(art, 'a', size=10)
        self._file(art, 'b', size=20)
        self.db.session.commit()
        s = self._summarise(art)
        self.assertTrue(s.has_files)
        self.assertFalse(s.has_references)
        self.assertEqual(s.unknown.count, 2)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
