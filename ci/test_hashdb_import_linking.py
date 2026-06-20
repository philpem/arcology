"""
Regression test: importing a hash database via the REST API must link the
existing collection to the new KnownFiles.

The web import route ran a hash-rescan after creating KnownFiles, but the API
bulk-add endpoint (used by `arco hashdb import`) did not — so a database
imported through the CLI showed zero matches in the hashdb search until a
manual rescan.  Both paths now share link_new_known_files().

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_import_linking -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-import-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_MD5 = 'bb' * 16
_SHA1 = 'cc' * 20


class TestHashdbImportLinking(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownProduct,
            Partition,
            StorageDirectory,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            # Active database + product, but NO KnownFiles yet (added via API).
            hdb = HashDatabase(name='Imported DB')
            db.session.add(hdb)
            db.session.flush()
            prod = KnownProduct(database_id=hdb.id, title='!Foo')
            db.session.add(prod)
            db.session.flush()
            cls.db_id, cls.product_id = hdb.id, prod.id

            # An already-extracted file in the collection, not yet known.
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
            ef = ExtractedFile(
                partition_id=part.id, path='!Foo/!RunImage', filename='!RunImage',
                md5=_MD5, sha1=_SHA1, file_size=123,
                is_directory=False)
            db.session.add(ef)
            db.session.flush()
            cls.ef_id = ef.id
            db.session.commit()

    def test_api_import_links_collection_and_search_finds_it(self):
        from myapp.database import ExtractedFile, KnownFile, KnownProduct

        # Before: the extracted file is unlinked.
        with self.app.app_context():
            ef = self.db.session.get(ExtractedFile, self.ef_id)
            self.assertFalse(ef.is_known)
            self.assertIsNone(ef.known_file_id)

        # Add the matching KnownFile through the API (the `arco hashdb import`
        # path).  WORKER_API_KEY authenticates as read_write.
        resp = self.client.post(
            f'/api/hash-databases/{self.db_id}/products/{self.product_id}/files',
            json={'files': [{
                'filename': '!RunImage',
                'file_size': 123,
                'md5': _MD5,
                'sha1': _SHA1,
                'is_required': True,
            }]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.get_json().get('added'), 1)

        # After: the existing extracted file is linked to the new KnownFile.
        with self.app.app_context():
            kf = KnownFile.query.filter_by(database_id=self.db_id, md5=_MD5).first()
            self.assertIsNotNone(kf)
            ef = self.db.session.get(ExtractedFile, self.ef_id)
            self.assertTrue(ef.is_known)
            self.assertEqual(ef.known_file_id, kf.id)

        # And the hashdb search now finds the match — the reported symptom was
        # this returning nothing.  Mirror the search view's core filter
        # (ExtractedFile linked to one of the product's KnownFiles).
        with self.app.app_context():
            product = self.db.session.get(KnownProduct, self.product_id)
            kf_ids = [k.id for k in product.known_files]
            matches = (
                ExtractedFile.query
                .filter(ExtractedFile.known_file_id.in_(kf_ids))
                .all()
            )
            self.assertEqual([m.id for m in matches], [self.ef_id])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
