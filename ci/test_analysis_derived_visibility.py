"""
Tests that /analysis/artefact/<uuid> does not leak private derived artefacts.

artefact_analyses() lists analyses for an artefact AND all of its derived
artefacts.  A derived artefact can be independently marked is_private (the edit
form allows it on any artefact, not just roots), so listing the whole derivation
tree without a per-artefact visibility check would expose a private derived
artefact's analyses — its label and its contribution to the status counts — to a
user who can view only the public parent.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_derived_visibility -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-analysis-vis-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_SECRET_LABEL = b'SECRET-DERIVED-ARTEFACT'


class TestAnalysisDerivedVisibility(unittest.TestCase):

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
            owner = User(username='ana-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            viewer = User(username='ana-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            admin = User(username='ana-admin', password_hash='x', is_admin=True,
                         permission=UserPermission.READ_WRITE)
            db.session.add_all([owner, viewer, admin])
            db.session.flush()
            cls.viewer_id, cls.admin_id = viewer.id, admin.id

            # Public item; public root artefact A; derived artefact B marked
            # is_private (owned by `owner`).
            item = Item(name='pub-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            root = Artefact(item_id=item.id, label='public-root',
                            artefact_type=ArtefactType.HFE, original_filename='r.hfe',
                            storage_path='r.hfe', owner_id=owner.id)
            db.session.add(root)
            db.session.flush()
            derived = Artefact(item_id=item.id, label='SECRET-DERIVED-ARTEFACT',
                               artefact_type=ArtefactType.RAW_SECTOR, original_filename='d.img',
                               storage_path='d.img', owner_id=owner.id,
                               parent_artefact_id=root.id, is_private=True)
            db.session.add(derived)
            db.session.flush()
            # A completed analysis on the PRIVATE derived artefact.
            db.session.add(Analysis(artefact_id=derived.id,
                                    analysis_type=AnalysisType.METADATA_EXTRACT,
                                    status=AnalysisStatus.COMPLETED, success=True))
            db.session.commit()
            cls.root_uuid = root.uuid

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_viewer_does_not_see_private_derived_analyses(self):
        self._login(self.viewer_id)
        r = self.client.get(f'/analysis/artefact/{self.root_uuid}')
        self.assertEqual(r.status_code, 200, r.data)
        # The private derived artefact's label must not appear, and its analysis
        # must not be counted (completed count stays 0 for the viewer).
        self.assertNotIn(_SECRET_LABEL, r.data)
        self.assertIn(b'Completed (0)', r.data)

    def test_admin_sees_derived_analyses(self):
        self._login(self.admin_id)
        r = self.client.get(f'/analysis/artefact/{self.root_uuid}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET_LABEL, r.data)
        self.assertIn(b'Completed (1)', r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
