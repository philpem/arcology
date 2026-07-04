"""
Tests the shared visible_derived_artefact_ids() helper.

The artefact view page and the analysis listing both aggregate an artefact and
its whole derived subtree (file listings, analyses, partitions, cautions).  A
derived artefact can be independently marked is_private even when its root is
public, so both paths must re-filter the derivation tree by visibility rather
than trusting the root's per-object guard.  visible_derived_artefact_ids() is
that single filter; this proves it excludes a private derived artefact for a
plain viewer while still returning it for the owner and for an admin.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_derived_artefact_visibility -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-derived-vis-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestVisibleDerivedArtefactIds(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import Artefact, Item, User, UserPermission
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner = User(username='dv-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            viewer = User(username='dv-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            admin = User(username='dv-admin', password_hash='x', is_admin=True,
                         permission=UserPermission.READ_WRITE)
            db.session.add_all([owner, viewer, admin])
            db.session.flush()
            cls.owner_id, cls.viewer_id, cls.admin_id = owner.id, viewer.id, admin.id

            # Public item; public root artefact; derived artefact marked private.
            item = Item(name='dv-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            root = Artefact(item_id=item.id, label='public-root',
                            artefact_type=ArtefactType.HFE, original_filename='r.hfe',
                            storage_path='r.hfe', owner_id=owner.id)
            db.session.add(root)
            db.session.flush()
            derived = Artefact(item_id=item.id, label='private-derived',
                               artefact_type=ArtefactType.RAW_SECTOR,
                               original_filename='d.img', storage_path='d.img',
                               owner_id=owner.id, parent_artefact_id=root.id,
                               is_private=True)
            db.session.add(derived)
            db.session.commit()
            cls.root_id, cls.derived_id = root.id, derived.id

    def _ids_for(self, uid):
        from myapp.database import Artefact, User
        from myapp.services.artefact_lifecycle import visible_derived_artefact_ids
        with self.app.app_context():
            root = self.db.session.get(Artefact, self.root_id)
            user = self.db.session.get(User, uid) if uid is not None else None
            return set(visible_derived_artefact_ids(root, user))

    def test_viewer_excludes_private_derived(self):
        ids = self._ids_for(self.viewer_id)
        self.assertIn(self.root_id, ids)
        self.assertNotIn(self.derived_id, ids)

    def test_anonymous_excludes_private_derived(self):
        ids = self._ids_for(None)
        self.assertIn(self.root_id, ids)
        self.assertNotIn(self.derived_id, ids)

    def test_owner_sees_private_derived(self):
        ids = self._ids_for(self.owner_id)
        self.assertEqual(ids, {self.root_id, self.derived_id})

    def test_admin_sees_private_derived(self):
        ids = self._ids_for(self.admin_id)
        self.assertEqual(ids, {self.root_id, self.derived_id})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
