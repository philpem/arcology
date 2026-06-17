"""
Regression test: deleting a hash database must be a small number of bulk SQL
statements, not an ORM cascade that loads every KnownFile/KnownProduct row and
walks an N+1 query over the product->files relationship (GitHub issue #618,
"Deleting hash databases takes a very long time").

Verifies that delete():
  - removes the database, its products, its known files, and any
    recognised_products rows that referenced those products (DB-level cascade);
  - unlinks ExtractedFile rows that pointed at the deleted known files
    (known_file_id -> NULL, is_known -> False) rather than orphaning them.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_delete -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-delete-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestHashdbDelete(unittest.TestCase):

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

        from myapp.database import User, UserPermission
        user = User(username='deleter', password_hash='x',
                    permission=UserPermission.READ_WRITE)
        db.session.add(user)
        db.session.commit()
        self.uid = user.id
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def test_delete_removes_cascade_and_unlinks_extracted_files(self):
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
            RecognisedProduct,
            StorageDirectory,
        )

        db = self.db

        hdb = HashDatabase(name='Arcarc Apps', is_active=True)
        db.session.add(hdb)
        db.session.flush()

        prod = KnownProduct(database_id=hdb.id, title='!Foo')
        db.session.add(prod)
        db.session.flush()

        kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                       filename='!RunImage', md5='bb' * 16, file_size=123)
        db.session.add(kf)
        db.session.flush()

        # Collection objects that reference the database's rows.
        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                         filesystem=FilesystemType.DFS)
        db.session.add(part)
        db.session.flush()

        ef = ExtractedFile(partition_id=part.id, path='!Foo/!RunImage',
                           filename='!RunImage', md5='bb' * 16, file_size=123,
                           is_directory=False, is_known=True, known_file_id=kf.id)
        db.session.add(ef)
        rp = RecognisedProduct(partition_id=part.id, product_id=prod.id,
                               folder_path='!Foo')
        db.session.add(rp)
        db.session.flush()

        db_id, ef_id = hdb.id, ef.id
        db.session.commit()

        resp = self.client.post(f'/hashdb/{db_id}/delete', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

        # Drop any identity-map cache so we re-read committed state from the DB.
        db.session.expire_all()

        # Database and all its dependent rows are gone.
        self.assertIsNone(db.session.get(HashDatabase, db_id))
        self.assertEqual(
            db.session.query(KnownFile).filter_by(database_id=db_id).count(), 0)
        self.assertEqual(
            db.session.query(KnownProduct).filter_by(database_id=db_id).count(), 0)
        self.assertEqual(db.session.query(RecognisedProduct).count(), 0)

        # The extracted file survives, but is unlinked and no longer "known".
        ef_after = db.session.get(ExtractedFile, ef_id)
        self.assertIsNotNone(ef_after)
        self.assertIsNone(ef_after.known_file_id)
        self.assertFalse(ef_after.is_known)

    def test_delete_product_removes_recognitions_and_unlinks(self):
        # A ubiquitous product (e.g. !System) recognised in many partitions:
        # deleting it must remove all its recognised_products rows and known
        # files, and unlink the extracted files that referenced them.
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
            RecognisedProduct,
            StorageDirectory,
        )

        db = self.db

        hdb = HashDatabase(name='Apps', is_active=True,
                           enable_product_recognition=False)
        db.session.add(hdb)
        db.session.flush()
        prod = KnownProduct(database_id=hdb.id, title='!System')
        db.session.add(prod)
        db.session.flush()
        kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                       filename='Modules', md5='cc' * 16, file_size=42)
        db.session.add(kf)
        db.session.flush()

        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()

        ef_ids = []
        # The product is recognised across many partitions.
        for i in range(5):
            part = Partition(artefact_id=art.id, partition_index=i, label=f'P{i}',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            db.session.add(RecognisedProduct(partition_id=part.id,
                                             product_id=prod.id,
                                             folder_path='!System'))
            ef = ExtractedFile(partition_id=part.id, path='!System/Modules',
                               filename='Modules', md5='cc' * 16, file_size=42,
                               is_directory=False, is_known=True,
                               known_file_id=kf.id)
            db.session.add(ef)
            db.session.flush()
            ef_ids.append(ef.id)

        db_id, pid = hdb.id, prod.id
        db.session.commit()

        resp = self.client.post(f'/hashdb/{db_id}/products/{pid}/delete',
                                follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))
        db.session.expire_all()

        self.assertIsNone(db.session.get(KnownProduct, pid))
        self.assertEqual(
            db.session.query(KnownFile).filter_by(product_id=pid).count(), 0)
        self.assertEqual(
            db.session.query(RecognisedProduct).filter_by(product_id=pid).count(), 0)
        # The database itself is untouched.
        self.assertIsNotNone(db.session.get(HashDatabase, db_id))
        # Extracted files survive but are unlinked.
        for ef_id in ef_ids:
            ef_after = db.session.get(ExtractedFile, ef_id)
            self.assertIsNotNone(ef_after)
            self.assertIsNone(ef_after.known_file_id)
            self.assertFalse(ef_after.is_known)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
