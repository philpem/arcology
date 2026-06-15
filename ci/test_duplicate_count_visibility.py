"""
Tests that the "identical files" duplicate-instance badge count matches the
number of instances actually listed by the duplicates page.

The file listing shows, per extracted file, a badge with the number of
globally-visible instances of that exact content (same size + SHA-256).
Clicking it opens file_duplicates, which lists those instances.

The badge count query in _view_file_listing() filters with
artefact_visibility_clause(), which references Item columns — so the query
*must* join Item.  It originally joined only Partition and Artefact, leaving
SQLAlchemy to add Item as an implicit cartesian join.  That multiplied
count(ExtractedFile.id) by the number of matching items, inflating the badge
far above the real count shown by the file_duplicates list (which joins Item
correctly).

These tests build several public items each holding one identical file, render
the artefact page as a non-owner READ_ONLY user, and assert the captured
duplicate_counts equals the true instance count — and that it matches the
number of rows the file_duplicates page would list.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_duplicate_count_visibility -v
"""

import os
import sys
import unittest
from contextlib import contextmanager

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-duplicate-count-test-key')
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


# Shared content key for the identical file replicated across every item.
_SIZE = 4096
_SHA = 'bb' * 32


class TestDuplicateCountVisibility(unittest.TestCase):

    N_PUBLIC = 3

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
            StorageDirectory,
            User,
            UserPermission,
        )
        from myapp.extensions import db
        from myapp.utils.privacy import recompute_item_privacy

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()

            owner = User(username='dup-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            viewer = User(username='dup-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            db.session.add_all([owner, viewer])
            db.session.flush()
            cls.viewer_id = viewer.id

            def _make_item_with_file(name, *, private):
                item = Item(name=name, is_private=private, owner_id=owner.id)
                db.session.add(item)
                db.session.flush()
                art = Artefact(item_id=item.id, label='Disc',
                               artefact_type=ArtefactType.HFE,
                               original_filename='d.ssd', storage_path=f'{name}.ssd',
                               storage_directory=StorageDirectory.UPLOADS,
                               owner_id=owner.id)
                db.session.add(art)
                db.session.flush()
                part = Partition(artefact_id=art.id, partition_index=0,
                                 label='Main', filesystem=FilesystemType.DFS)
                db.session.add(part)
                db.session.flush()
                db.session.add(ExtractedFile(
                    partition_id=part.id, path='DUP', filename='DUP',
                    file_size=_SIZE, sha256=_SHA, is_directory=False))
                recompute_item_privacy(item)
                return item, art

            # N public items, each holding one identical file.
            first_item, first_art = _make_item_with_file('pub-0', private=False)
            cls.view_item_uuid = first_item.uuid
            cls.view_art_uuid = first_art.uuid
            for i in range(1, cls.N_PUBLIC):
                _make_item_with_file(f'pub-{i}', private=False)

            # One additional PRIVATE item with the same file — the viewer must
            # NOT be able to see or count this instance.
            _priv_item, priv_art = _make_item_with_file('priv', private=True)
            priv_part = Partition.query.filter_by(artefact_id=priv_art.id).first()
            cls.priv_file_uuid = ExtractedFile.query.filter_by(
                partition_id=priv_part.id).first().uuid

            db.session.commit()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_badge_count_matches_visible_instances(self):
        """The badge count equals the number of public instances, not a
        cartesian-inflated value, and excludes the private instance."""
        self._login(self.viewer_id)
        url = f'/items/{self.view_item_uuid}/artefacts/{self.view_art_uuid}'
        with captured_templates(self.app) as templates:
            r = self.client.get(url, follow_redirects=True)
            self.assertEqual(r.status_code, 200, r.data[:500])
            ctx = next(c for _t, c in templates if 'duplicate_counts' in c)
        self.assertEqual(ctx['duplicate_counts'].get((_SIZE, _SHA)), self.N_PUBLIC)

    def test_badge_count_matches_duplicates_list_length(self):
        """The badge count is consistent with the file_duplicates list the
        viewer actually sees when clicking through."""
        from myapp.database import ExtractedFile
        with self.app.app_context():
            file_uuid = ExtractedFile.query.filter_by(
                file_size=_SIZE, sha256=_SHA).first().uuid

        self._login(self.viewer_id)
        # Badge count from the listing page.
        url = f'/items/{self.view_item_uuid}/artefacts/{self.view_art_uuid}'
        with captured_templates(self.app) as templates:
            self.client.get(url, follow_redirects=True)
            ctx = next(c for _t, c in templates if 'duplicate_counts' in c)
        badge_count = ctx['duplicate_counts'].get((_SIZE, _SHA))

        # Instances actually listed by file_duplicates.
        with captured_templates(self.app) as templates:
            r = self.client.get(f'/files/{file_uuid}/duplicates', follow_redirects=True)
            self.assertEqual(r.status_code, 200, r.data[:500])
            dup_ctx = next(c for _t, c in templates if 'instances' in c)
        self.assertEqual(badge_count, len(dup_ctx['instances']))

    def test_duplicates_page_404_for_file_in_private_artefact(self):
        """The duplicates page must not leak the source file (path, size,
        artefact label) of a file inside a private artefact the viewer cannot
        see — even though other public items hold identical content."""
        self._login(self.viewer_id)
        r = self.client.get(
            f'/files/{self.priv_file_uuid}/duplicates', follow_redirects=True)
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
