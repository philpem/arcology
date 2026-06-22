"""
Tests for create_hashdb_from_artefacts (myapp/services/hash_rescan.py) — the
"snapshot an artefact's files into a base-system hash database" feature.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_from_artefact -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-from-artefact-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _h(seed: str, n: int) -> str:
    return (seed * n)[:n]


def _add_artefact(db, item, label, files):
    """files: list of (path, seed). md5 + sha256 derived from seed; None seed => no hash."""
    from arcology_shared.enums import ArtefactType
    from myapp.database import Artefact, ExtractedFile, FilesystemType, Partition
    art = Artefact(item_id=item.id, label=label, artefact_type=ArtefactType.RAW_SECTOR,
                   original_filename=f'{label}.img', storage_path=f'uploads/{label}.img')
    db.session.add(art)
    db.session.flush()
    part = Partition(artefact_id=art.id, partition_index=0, filesystem=FilesystemType.ADFS)
    db.session.add(part)
    db.session.flush()
    for path, seed in files:
        kw = {}
        if seed is not None:
            kw = {'md5': _h(seed, 32), 'sha1': _h(seed, 40), 'sha256': _h(seed, 64)}
        db.session.add(ExtractedFile(
            partition_id=part.id, path=path, filename=path.split('/')[-1],
            file_size=1000, is_directory=False, **kw))
    db.session.commit()
    return art


class TestCreateHashdbFromArtefact(unittest.TestCase):

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
        from myapp.database import (
            Artefact,
            ExtractedFile,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            Platform,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        for model in (KnownFile, KnownProduct, HashDatabase, ExtractedFile,
                      Partition, Artefact, Item, Platform):
            model.query.delete()
        self.db.session.commit()
        self.item = Item(name='Coll')
        self.db.session.add(self.item)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def test_snapshots_all_files_deduped(self):
        from myapp.database import KnownFile, KnownProduct
        from myapp.services.hash_rescan import create_hashdb_from_artefacts
        art = _add_artefact(self.db, self.item, 'J233', [
            ('!System/A', 'a'), ('!System/B', 'b'),
            ('Library/C', 'c'), ('dup', 'a'),  # 'dup' shares content with !System/A
        ])
        database, added, skipped = create_hashdb_from_artefacts(
            'Base RISC OS', [art.id], exclude_from_similarity=True)
        self.assertEqual(skipped, 0)
        # 4 files but only 3 unique content hashes.
        self.assertEqual(added, 3)
        self.assertEqual(KnownFile.query.filter_by(database_id=database.id).count(), 3)
        self.assertTrue(database.exclude_from_similarity)
        self.assertEqual(KnownProduct.query.filter_by(database_id=database.id).count(), 1)

    def test_files_without_hash_skipped(self):
        from myapp.services.hash_rescan import create_hashdb_from_artefacts
        art = _add_artefact(self.db, self.item, 'X', [('f', 'f'), ('nohash', None)])
        _database, added, skipped = create_hashdb_from_artefacts('DB1', [art.id])
        self.assertEqual(added, 1)
        self.assertEqual(skipped, 1)

    def test_duplicate_name_rejected(self):
        from myapp.services.hash_rescan import create_hashdb_from_artefacts
        art = _add_artefact(self.db, self.item, 'X', [('f', 'f')])
        create_hashdb_from_artefacts('Dupe', [art.id])
        with self.assertRaises(ValueError):
            create_hashdb_from_artefacts('dupe', [art.id])  # case-insensitive clash

    def test_blank_name_rejected(self):
        from myapp.services.hash_rescan import create_hashdb_from_artefacts
        art = _add_artefact(self.db, self.item, 'X', [('f', 'f')])
        with self.assertRaises(ValueError):
            create_hashdb_from_artefacts('   ', [art.id])

    def test_links_collection_to_snapshot(self):
        """After creation, a matching file on another artefact links to the DB."""
        from myapp.database import ExtractedFile, KnownFile, Partition
        from myapp.services.hash_rescan import create_hashdb_from_artefacts
        base = _add_artefact(self.db, self.item, 'Base', [('!System/Mod', 'os')])
        other = _add_artefact(self.db, self.item, 'Other', [('!System/Mod', 'os'), ('game', 'g')])
        create_hashdb_from_artefacts('Base OS', [base.id], exclude_from_similarity=True)
        # The shared OS file on 'other' should now be linked to a KnownFile.
        shared = (ExtractedFile.query
                  .join(Partition, ExtractedFile.partition_id == Partition.id)
                  .filter(Partition.artefact_id == other.id,
                          ExtractedFile.path == '!System/Mod')
                  .one())
        self.assertIsNotNone(shared.known_file_id)
        kf = self.db.session.get(KnownFile, shared.known_file_id)
        self.assertEqual(kf.sha256, _h('os', 64))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
