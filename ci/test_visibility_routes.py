"""
Visibility enforcement tests for web routes.

Covers the require_visible_item decorator (myapp/permissions.py) and the
visibility filtering added by the route audit:

  - Private items 404 (not 403) for users without access; contribute-gated
    routes 403 for viewer-share holders.
  - The analysis blueprint hides analyses on private artefacts from the
    index, queue, artefact listing, and detail/cancel/retry routes.
  - The hash database collection search excludes matches inside private
    items.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_visibility_routes -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-visibility-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_PASSWORD = 'testpassword1234'


class VisibilityRouteTestBase(unittest.TestCase):
    """App with one private and one public item, each carrying an analysis."""

    @classmethod
    def setUpClass(cls):
        import bcrypt
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()

            from arcology_shared.enums import AnalysisType, ArtefactType
            from myapp.database import (
                Analysis,
                AnalysisStatus,
                Artefact,
                ExtractedFile,
                FilesystemType,
                HashDatabase,
                Item,
                ItemShare,
                KnownFile,
                Partition,
                User,
                UserPermission,
            )
            from myapp.utils.privacy import recompute_item_privacy

            pw = bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt()).decode()

            def make_user(username, *, is_admin=False, permission=UserPermission.READ_WRITE):
                user = User(username=username, password_hash=pw,
                            is_admin=is_admin, permission=permission)
                _db.session.add(user)
                _db.session.flush()
                return user

            owner = make_user('vis-owner')
            make_user('vis-other')
            viewer = make_user('vis-viewer')
            make_user('vis-admin', is_admin=True)

            def make_item(name, *, owner_id=None, is_private=False):
                item = Item(name=name, owner_id=owner_id, is_private=is_private)
                _db.session.add(item)
                _db.session.flush()
                recompute_item_privacy(item)
                return item

            private_item = make_item('Private Collection', owner_id=owner.id, is_private=True)
            public_item = make_item('Public Collection')

            # Viewer-level share on the private item: may view, must not modify
            _db.session.add(ItemShare(item_id=private_item.id, user_id=viewer.id,
                                      permission='viewer'))

            def make_artefact(item, label):
                artefact = Artefact(
                    item_id=item.id, label=label,
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=f'{label}.adf', storage_path=f'{label}.adf',
                )
                _db.session.add(artefact)
                _db.session.flush()
                return artefact

            private_art = make_artefact(private_item, 'private-disc')
            public_art = make_artefact(public_item, 'public-disc')

            def make_analysis(artefact, status=AnalysisStatus.PENDING):
                analysis = Analysis(artefact_id=artefact.id,
                                    analysis_type=AnalysisType.CHECKSUM_COMPUTE,
                                    status=status)
                _db.session.add(analysis)
                _db.session.flush()
                return analysis

            private_analysis = make_analysis(private_art)
            public_analysis = make_analysis(public_art)

            # Hash DB with a known file matched by one extracted file in each item
            hashdb = HashDatabase(name='Vis Test DB')
            _db.session.add(hashdb)
            _db.session.flush()
            kf = KnownFile(database_id=hashdb.id, filename='KNOWNFILE', md5='a' * 32)
            _db.session.add(kf)
            _db.session.flush()

            def make_extracted(artefact, path):
                partition = Partition(artefact_id=artefact.id,
                                      filesystem=FilesystemType.UNKNOWN)
                _db.session.add(partition)
                _db.session.flush()
                ef = ExtractedFile(partition_id=partition.id, path=path,
                                   filename=path, known_file_id=kf.id, is_known=True)
                _db.session.add(ef)
                return ef

            make_extracted(private_art, 'private-match')
            make_extracted(public_art, 'public-match')

            _db.session.commit()

            cls.private_item_uuid = private_item.uuid
            cls.public_item_uuid = public_item.uuid
            cls.private_art_uuid = private_art.uuid
            cls.private_analysis_uuid = private_analysis.uuid
            cls.public_analysis_uuid = public_analysis.uuid
            cls.hashdb_id = hashdb.id

    def client_for(self, username):
        """Return a fresh test client logged in as *username*."""
        client = self.app.test_client()
        resp = client.post('/login', data={'username': username, 'password': _PASSWORD})
        assert resp.status_code in (302, 303), f'login failed: {resp.status_code}'
        return client


class TestRequireVisibleItem(VisibilityRouteTestBase):
    """Decorator-converted items.py routes enforce visibility."""

    def test_private_item_404_for_other_user(self):
        client = self.client_for('vis-other')
        self.assertEqual(client.get(f'/items/{self.private_item_uuid}').status_code, 404)

    def test_private_item_200_for_owner(self):
        # follow_redirects: the route 301s from the raw UUID to the slug URL
        client = self.client_for('vis-owner')
        resp = client.get(f'/items/{self.private_item_uuid}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_private_item_200_for_admin(self):
        client = self.client_for('vis-admin')
        resp = client.get(f'/items/{self.private_item_uuid}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_private_item_200_for_viewer_share(self):
        client = self.client_for('vis-viewer')
        resp = client.get(f'/items/{self.private_item_uuid}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_edit_private_item_404_for_other_user(self):
        client = self.client_for('vis-other')
        self.assertEqual(client.get(f'/items/{self.private_item_uuid}/edit').status_code, 404)

    def test_edit_private_item_403_for_viewer_share(self):
        """A viewer share may see the item but contribute-gated routes 403."""
        client = self.client_for('vis-viewer')
        self.assertEqual(client.get(f'/items/{self.private_item_uuid}/edit').status_code, 403)

    def test_public_item_200_for_any_user(self):
        client = self.client_for('vis-other')
        resp = client.get(f'/items/{self.public_item_uuid}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)


class TestAnalysisVisibility(VisibilityRouteTestBase):
    """Analysis routes hide analyses on artefacts the caller may not view."""

    def test_index_hides_private_artefact(self):
        client = self.client_for('vis-other')
        resp = client.get('/analysis/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'private-disc', resp.data)
        self.assertIn(b'public-disc', resp.data)

    def test_index_shows_private_artefact_to_owner(self):
        client = self.client_for('vis-owner')
        resp = client.get('/analysis/')
        self.assertIn(b'private-disc', resp.data)

    def test_queue_hides_private_artefact(self):
        client = self.client_for('vis-other')
        resp = client.get('/analysis/queue')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'private-disc', resp.data)

    def test_detail_404_for_other_user(self):
        client = self.client_for('vis-other')
        self.assertEqual(
            client.get(f'/analysis/{self.private_analysis_uuid}').status_code, 404)

    def test_detail_200_for_owner(self):
        client = self.client_for('vis-owner')
        self.assertEqual(
            client.get(f'/analysis/{self.private_analysis_uuid}').status_code, 200)

    def test_artefact_listing_404_for_other_user(self):
        client = self.client_for('vis-other')
        self.assertEqual(
            client.get(f'/analysis/artefact/{self.private_art_uuid}').status_code, 404)

    def test_cancel_404_for_other_user(self):
        client = self.client_for('vis-other')
        resp = client.post(f'/analysis/{self.private_analysis_uuid}/cancel')
        self.assertEqual(resp.status_code, 404)
        with self.app.app_context():
            from myapp.database import Analysis
            self.assertIsNotNone(
                Analysis.query.filter_by(uuid=self.private_analysis_uuid).first(),
                'analysis must not have been cancelled')

    def test_cancel_403_for_viewer_share(self):
        client = self.client_for('vis-viewer')
        resp = client.post(f'/analysis/{self.private_analysis_uuid}/cancel')
        self.assertEqual(resp.status_code, 403)

    def test_retry_404_for_other_user(self):
        client = self.client_for('vis-other')
        resp = client.post(f'/analysis/{self.private_analysis_uuid}/retry')
        self.assertEqual(resp.status_code, 404)

    def test_public_analysis_detail_200_for_other_user(self):
        client = self.client_for('vis-other')
        self.assertEqual(
            client.get(f'/analysis/{self.public_analysis_uuid}').status_code, 200)


class TestHashdbSearchVisibility(VisibilityRouteTestBase):
    """Hash DB collection search must not enumerate private items."""

    def test_search_hides_private_matches(self):
        client = self.client_for('vis-other')
        resp = client.get(f'/hashdb/{self.hashdb_id}/search')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'private-match', resp.data)
        self.assertNotIn(b'Private Collection', resp.data)
        self.assertIn(b'public-match', resp.data)

    def test_search_shows_private_matches_to_owner(self):
        client = self.client_for('vis-owner')
        resp = client.get(f'/hashdb/{self.hashdb_id}/search')
        self.assertIn(b'private-match', resp.data)

    def test_search_shows_private_matches_to_admin(self):
        client = self.client_for('vis-admin')
        resp = client.get(f'/hashdb/{self.hashdb_id}/search')
        self.assertIn(b'private-match', resp.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
