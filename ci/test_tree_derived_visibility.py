"""
Tests that the derivation-tree aggregators do not leak private derived artefacts.

A derived artefact can be independently marked ``is_private`` even when its root
is public.  The tree/aggregator endpoints walk the whole derivation subtree, so
without a per-artefact visibility re-filter they leak a private child's metadata
(label, UUID, original filename, analysis rows) to a user who can only see the
public root:

  * web  ``/artefacts/<uuid>/tree``            (build_processing_tree)
  * API  ``/api/artefacts/<uuid>/analysis/tree``      (analysis_tree_node)
  * API  ``/api/artefacts/<uuid>/processing-tree``    (build_processing_tree)
  * API  ``/api/artefacts/<uuid>/analysis/recursive`` (collect_all_analyses)

Also covers the private-item-name leak via ``/items/new?parent=<prefix>``: the
preset-parent breadcrumb rendered a private parent's name without a visibility
check, enumerable by short UUID prefix.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_tree_derived_visibility -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-tree-vis-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_SECRET_LABEL = b'SECRET-DERIVED-ARTEFACT'
_SECRET_FILENAME = b'secret-derived.img'


def _make_user(db, username, permission, is_admin=False):
    from myapp.database import ApiKey, ApiKeyPermission, User
    user = User(username=username, password_hash='x', is_admin=is_admin,
                permission=permission, can_use_api=True)
    db.session.add(user)
    db.session.flush()
    key_obj, raw = ApiKey.create(user_id=user.id, name=f'{username}-key',
                                 permission=ApiKeyPermission.READ_ONLY)
    db.session.add(key_obj)
    db.session.commit()
    return user, raw


class TestTreeDerivedVisibility(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            Artefact,
            Item,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner, _ = _make_user(db, 'tree-owner', UserPermission.READ_WRITE)
            viewer, cls.viewer_key = _make_user(db, 'tree-viewer', UserPermission.READ_ONLY)
            admin, cls.admin_key = _make_user(db, 'tree-admin', UserPermission.READ_WRITE,
                                              is_admin=True)
            # A read_write non-owner who may reach /items/new but must not be
            # able to view the private parent item.
            stranger, _ = _make_user(db, 'tree-stranger', UserPermission.READ_WRITE)
            cls.viewer_id, cls.admin_id, cls.stranger_id = viewer.id, admin.id, stranger.id

            # Public item; public root artefact; derived artefact marked private.
            item = Item(name='pub-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            root = Artefact(item_id=item.id, label='public-root',
                            artefact_type=ArtefactType.HFE, original_filename='r.hfe',
                            storage_path='r.hfe', owner_id=owner.id)
            db.session.add(root)
            db.session.flush()
            # An analysis on the (visible) root that produced the private child —
            # this is the edge analysis_tree_node descends through.
            root_analysis = Analysis(artefact_id=root.id,
                                     analysis_type=AnalysisType.FLUX_DECODE,
                                     status=AnalysisStatus.COMPLETED, success=True)
            db.session.add(root_analysis)
            db.session.flush()
            derived = Artefact(item_id=item.id, label='SECRET-DERIVED-ARTEFACT',
                               artefact_type=ArtefactType.RAW_SECTOR,
                               original_filename='secret-derived.img',
                               storage_path='d.img', owner_id=owner.id,
                               parent_artefact_id=root.id,
                               derived_from_analysis_id=root_analysis.id, is_private=True)
            db.session.add(derived)
            db.session.flush()
            db.session.add(Analysis(artefact_id=derived.id,
                                    analysis_type=AnalysisType.METADATA_EXTRACT,
                                    status=AnalysisStatus.COMPLETED, success=True))

            # A separate private item, to check the /items/new?parent= leak.
            priv_item = Item(name='TOP-SECRET-COLLECTION', owner_id=owner.id,
                             is_private=True)
            db.session.add(priv_item)
            db.session.flush()
            from myapp.blueprints.items import recompute_item_privacy
            recompute_item_privacy(priv_item)
            db.session.commit()
            cls.root_uuid = root.uuid
            cls.priv_item_uuid = priv_item.uuid

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def _logout(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    # --- web processing tree ------------------------------------------------

    def test_web_tree_hides_private_derived_from_viewer(self):
        self._login(self.viewer_id)
        r = self.client.get(f'/artefacts/{self.root_uuid}/tree')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(_SECRET_LABEL, r.data)

    def test_web_tree_shows_private_derived_to_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/artefacts/{self.root_uuid}/tree')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET_LABEL, r.data)

    # --- API analysis/tree, processing-tree, analysis/recursive -------------

    def _api_get(self, path, key):
        return self.client.get(path, headers={'X-API-Key': key})

    def test_api_analysis_tree_hides_private_derived(self):
        r = self._api_get(f'/api/artefacts/{self.root_uuid}/analysis/tree', self.viewer_key)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(_SECRET_LABEL, r.data)
        self.assertNotIn(_SECRET_FILENAME, r.data)

    def test_api_processing_tree_hides_private_derived(self):
        r = self._api_get(f'/api/artefacts/{self.root_uuid}/processing-tree', self.viewer_key)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(_SECRET_LABEL, r.data)

    def test_api_analysis_recursive_hides_private_derived(self):
        r = self._api_get(f'/api/artefacts/{self.root_uuid}/analysis/recursive', self.viewer_key)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(_SECRET_LABEL, r.data)
        # Only the root's own analysis is visible; the private derived artefact's
        # analysis must not be counted for the viewer.
        self.assertEqual(r.get_json()['total'], 1)

    def test_api_trees_show_private_derived_to_admin(self):
        for path in ('analysis/tree', 'processing-tree', 'analysis/recursive'):
            r = self._api_get(f'/api/artefacts/{self.root_uuid}/{path}', self.admin_key)
            self.assertEqual(r.status_code, 200, r.data)
            self.assertIn(_SECRET_LABEL, r.data, path)

    # --- /items/new?parent= preset-parent name leak -------------------------

    def test_items_new_does_not_leak_private_parent_name(self):
        self._login(self.stranger_id)
        prefix = self.priv_item_uuid[:8]
        r = self.client.get(f'/items/new?parent={prefix}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(b'TOP-SECRET-COLLECTION', r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
