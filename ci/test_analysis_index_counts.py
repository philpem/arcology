"""
Tests the /analysis/ index page count/listing split.

The status counts are global operational-load totals (consistent with the queue
page) — they expose only aggregate job counts, not identifiable content — while
the listed analysis rows remain filtered to artefacts the caller may view.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_index_counts -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-analysis-counts-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_PRIVATE_LABEL = b'PRIVATE-ARTEFACT-LABEL'


class TestAnalysisIndexCounts(unittest.TestCase):

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
        from myapp.utils.privacy import recompute_item_privacy

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner = User(username='cnt-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            viewer = User(username='cnt-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            db.session.add_all([owner, viewer])
            db.session.flush()
            cls.viewer_id = viewer.id

            # A PRIVATE item the viewer cannot see, with one completed analysis.
            item = Item(name='private-item', is_private=True, owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='PRIVATE-ARTEFACT-LABEL',
                           artefact_type=ArtefactType.HFE, original_filename='p.hfe',
                           storage_path='p.hfe', owner_id=owner.id)
            db.session.add(art)
            db.session.flush()
            db.session.add(Analysis(artefact_id=art.id,
                                    analysis_type=AnalysisType.METADATA_EXTRACT,
                                    status=AnalysisStatus.COMPLETED, success=True))
            recompute_item_privacy(item)
            db.session.commit()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_counts_are_global_but_rows_are_filtered(self):
        self._login(self.viewer_id)
        r = self.client.get('/analysis/')
        self.assertEqual(r.status_code, 200, r.data)
        # Global count includes the private item's analysis...
        self.assertIn(b'Completed (1)', r.data)
        # ...but the private artefact's row must not be listed.
        self.assertNotIn(_PRIVATE_LABEL, r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
