"""
Regression tests for denormalised counters that could drift from the rows they
summarise (GitHub issue #637), the same class of bug as the removed ``is_known``
column:

* ``Partition.total_files`` must not double-count.  It used to be seeded at
  partition registration *and* incremented per file batch; it is now owned by
  the incremental add_files() counter alone and recomputed (alongside
  ``unique_files``) by the hash rescan.
* ``HashDatabase.file_count`` must stay in step when a whole product is deleted.
  The bulk known-files delete used to leave it overcounting.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_denormalised_counters -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-denormalised-counters-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class _CounterTestBase(unittest.TestCase):

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.db = db

        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()


class TestPartitionTotalFiles(_CounterTestBase):

    def _make_artefact(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        db = self.db
        item = Item(name='disc', is_private=False)
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.commit()
        return art.uuid

    def test_register_then_add_does_not_double_count(self):
        """Registering a partition and posting its files must leave total_files
        equal to the number of rows, not twice that."""
        from myapp.database import Partition
        auuid = self._make_artefact()
        hdr = {'X-API-Key': _WORKER_KEY}

        # The worker no longer sends total_files; even if a stale worker does,
        # the web app must ignore it rather than double-count.
        resp = self.client.post(
            f'/api/artefacts/{auuid}/partitions',
            json={'partition_index': 0, 'filesystem': 'dfs', 'total_files': 3},
            headers=hdr)
        self.assertEqual(resp.status_code, 201, resp.data)
        puuid = resp.get_json()['uuid']

        files = [{'path': f'F{i}', 'filename': f'F{i}', 'is_directory': False,
                  'md5': f'{i:032x}'} for i in range(3)]
        resp = self.client.post(f'/api/partitions/{puuid}/files',
                                json={'files': files}, headers=hdr)
        self.assertEqual(resp.status_code, 200, resp.data)

        self.db.session.expire_all()
        part = self.db.session.scalars(self.db.select(Partition)).first()
        self.assertEqual(part.total_files, 3)
        self.assertEqual(part.unique_files, 3)

    def test_rescan_recomputes_drifted_total_files(self):
        """When the hash rescan touches a partition, it recomputes total_files
        (and unique_files) from the rows, healing any prior drift."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            Partition,
            StorageDirectory,
        )
        from myapp.services.hash_rescan import rescan_hashes_for_artefact
        db = self.db

        # An active known file so the rescan actually links a row (and thus
        # refreshes the partition's counters).
        hdb = HashDatabase(name='Apps', is_active=True)
        db.session.add(hdb)
        db.session.flush()
        db.session.add(KnownFile(database_id=hdb.id, filename='F0',
                                 md5=f'{0:032x}', file_size=None))

        item = Item(name='disc2', is_private=False)
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d2.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                         filesystem=FilesystemType.DFS,
                         total_files=999, unique_files=999)  # drifted
        db.session.add(part)
        db.session.flush()
        for i in range(2):
            db.session.add(ExtractedFile(
                partition_id=part.id, path=f'F{i}', filename=f'F{i}',
                md5=f'{i:032x}', is_directory=False))
        db.session.commit()

        rescan_hashes_for_artefact(art)

        db.session.expire_all()
        part = db.session.get(Partition, part.id)
        self.assertEqual(part.total_files, 2)   # recomputed from 2 rows
        self.assertEqual(part.unique_files, 1)  # one row now linked to a known file


class TestHashDatabaseFileCount(_CounterTestBase):

    def setUp(self):
        super().setUp()
        from myapp.database import User, UserPermission
        user = User(username='curator', password_hash='x',
                    permission=UserPermission.READ_WRITE, can_use_api=True)
        self.db.session.add(user)
        self.db.session.commit()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
            sess['_fresh'] = True

    def test_product_delete_keeps_file_count_accurate(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            StorageDirectory,
        )
        db = self.db

        hdb = HashDatabase(name='Apps', is_active=True)
        db.session.add(hdb)
        db.session.flush()
        prod_a = KnownProduct(database_id=hdb.id, title='!Foo')
        prod_b = KnownProduct(database_id=hdb.id, title='!Bar')
        db.session.add_all([prod_a, prod_b])
        db.session.flush()

        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='c.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                         filesystem=FilesystemType.DFS)
        db.session.add(part)
        db.session.flush()

        # Product A: 2 known files (with a linked extracted file each).
        for i in range(2):
            kf = KnownFile(database_id=hdb.id, product_id=prod_a.id,
                           filename=f'!FooRun{i}', md5=f'a{i:031x}', file_size=10)
            db.session.add(kf)
            db.session.flush()
            db.session.add(ExtractedFile(
                partition_id=part.id, path=f'!Foo/Run{i}', filename=f'Run{i}',
                md5=f'a{i:031x}', file_size=10, is_directory=False,
                known_file_id=kf.id))
        # Product B: 1 known file.
        kfb = KnownFile(database_id=hdb.id, product_id=prod_b.id,
                        filename='!BarRun', md5='b' * 32, file_size=20)
        db.session.add(kfb)
        db.session.commit()
        db_id, pid_a = hdb.id, prod_a.id

        resp = self.client.post(f'/hashdb/{db_id}/products/{pid_a}/delete',
                                follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

        db.session.expire_all()
        hdb = db.session.get(HashDatabase, db_id)
        remaining = KnownFile.query.filter_by(database_id=db_id).count()
        self.assertEqual(remaining, 1)
        self.assertEqual(hdb.file_count, 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
