"""
Tests that hash-database match counts respect artefact visibility.

The hashdb 'view' and 'view_product' pages show, per known file/product, how
many times it occurs in the collection.  search() already filters those
occurrences by visibility (so a hash search cannot enumerate private
collections), but the match-count queries did not — so the counts revealed that
a known file exists inside a private artefact a user cannot see.

These tests render the pages as a non-owner READ_ONLY user vs an admin and
assert on the match_counts passed to the template (captured via the
template_rendered signal — no HTML parsing).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_count_visibility -v
"""

import os
import sys
import unittest
from contextlib import contextmanager

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-count-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


@contextmanager
def captured_templates(app):
    from flask import template_rendered
    recorded = []

    def record(sender, template, context, **extra):
        recorded.append((template, context))

    template_rendered.connect(record, app)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(record, app)


class TestHashdbCountVisibility(unittest.TestCase):

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
            KnownFile,
            KnownProduct,
            Partition,
            ProductRecognitionStatus,
            RecognisedProduct,
            StorageDirectory,
            User,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()

            owner = User(username='count-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            viewer = User(username='count-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            admin = User(username='count-admin', password_hash='x', is_admin=True,
                         permission=UserPermission.READ_WRITE)
            db.session.add_all([owner, viewer, admin])
            db.session.flush()
            cls.viewer_id, cls.admin_id = viewer.id, admin.id

            hdb = HashDatabase(
                name='Test HashDB',
                enable_product_recognition=True,
                product_recognition_status=ProductRecognitionStatus.COMPLETED,
            )
            db.session.add(hdb)
            db.session.flush()
            prod = KnownProduct(database_id=hdb.id, title='Secret Product')
            db.session.add(prod)
            db.session.flush()
            kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                           filename='SecretFile', md5='aa' * 16)
            db.session.add(kf)
            db.session.flush()
            cls.db_id, cls.product_id, cls.kf_id = hdb.id, prod.id, kf.id

            # A PRIVATE item owned by `owner`, containing one file that matches
            # the known file — the only occurrence in the collection.
            item = Item(name='private-coll', is_private=True, owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            from myapp.utils.privacy import recompute_item_privacy
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS, owner_id=owner.id)
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            db.session.add(ExtractedFile(
                partition_id=part.id, path='SecretFile', filename='SecretFile',
                md5='aa' * 16, is_directory=False, known_file_id=kf.id))
            db.session.add(RecognisedProduct(
                partition_id=part.id, product_id=prod.id, folder_path='/',
                required_matched=1, required_total=1,
                optional_matched=0, optional_total=0))
            recompute_item_privacy(item)
            db.session.commit()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def _context_for(self, url, uid):
        self._login(uid)
        with captured_templates(self.app) as templates:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, r.data)
            self.assertTrue(templates, 'no template rendered')
            return templates[-1][1]

    def _counts_for(self, url, uid):
        return self._context_for(url, uid)['match_counts']

    # The view page now aggregates per-product (the per-file table is loaded
    # lazily via product_files); assert the product-level count is filtered.
    def test_view_product_count_hidden_from_non_owner(self):
        ctx = self._context_for(f'/hashdb/{self.db_id}', self.viewer_id)
        # The only match is inside a private artefact the viewer cannot see.
        self.assertEqual(ctx['product_recognition_counts'].get(self.product_id, 0), 0)

    def test_view_product_count_visible_to_admin(self):
        ctx = self._context_for(f'/hashdb/{self.db_id}', self.admin_id)
        self.assertEqual(ctx['product_recognition_counts'].get(self.product_id, 0), 1)

    # The lazily-loaded per-file fragment must apply the same visibility filter.
    def test_product_files_count_hidden_from_non_owner(self):
        url = f'/hashdb/{self.db_id}/products/{self.product_id}/files'
        counts = self._counts_for(url, self.viewer_id)
        self.assertEqual(counts.get(self.kf_id, 0), 0)

    def test_product_files_count_visible_to_admin(self):
        url = f'/hashdb/{self.db_id}/products/{self.product_id}/files'
        counts = self._counts_for(url, self.admin_id)
        self.assertEqual(counts.get(self.kf_id, 0), 1)

    def test_view_product_page_count_hidden_from_non_owner(self):
        counts = self._counts_for(f'/hashdb/{self.db_id}/{self.product_id}', self.viewer_id)
        self.assertEqual(counts.get(self.kf_id, 0), 0)

    def test_view_product_page_count_visible_to_admin(self):
        counts = self._counts_for(f'/hashdb/{self.db_id}/{self.product_id}', self.admin_id)
        self.assertEqual(counts.get(self.kf_id, 0), 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
