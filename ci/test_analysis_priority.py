"""
Admin/staff reprioritise controls for PENDING analyses.

Covers the single-job / artefact / item priority routes: PENDING rows are
updated, non-PENDING rows are rejected/untouched, visibility and content-rights
gates are enforced (read_only -> 403; private artefacts excluded), and bulk
scopes only touch the artefacts the caller may manage.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_priority -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-analysis-priority-secret-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestAnalysisPriority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def setUp(self):
        from myapp.database import Analysis, Artefact, Item, User
        self._ctx = self.app.app_context()
        self._ctx.push()
        Analysis.query.delete()
        Artefact.query.delete()
        Item.query.delete()
        User.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self._ctx.pop()

    # -- fixtures ----------------------------------------------------------

    def _user(self, name, permission, is_admin=False):
        from myapp.database import User, UserPermission
        u = User(username=name, password_hash='x', is_admin=is_admin,
                 permission=getattr(UserPermission, permission))
        self.db.session.add(u)
        self.db.session.flush()
        return u

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def _artefact(self, owner_id, label='a', is_private=False, item=None,
                  parent_id=None):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        if item is None:
            item = Item(name=f'Item {label}', owner_id=owner_id)
            self.db.session.add(item)
            self.db.session.flush()
        art = Artefact(
            item_id=item.id, label=label, artefact_type=ArtefactType.SCP,
            original_filename=f'{label}.scp', storage_path=f'{label}.scp',
            storage_directory=StorageDirectory.UPLOADS, owner_id=owner_id,
            is_private=is_private, parent_artefact_id=parent_id)
        self.db.session.add(art)
        self.db.session.flush()
        return art, item

    def _analysis(self, artefact_id, status, priority=0):
        from arcology_shared.enums import AnalysisStatus, AnalysisType
        from myapp.database import Analysis
        st = getattr(AnalysisStatus, status)
        a = Analysis(artefact_id=artefact_id, analysis_type=AnalysisType.CHECKSUM_COMPUTE,
                     status=st, priority=priority)
        self.db.session.add(a)
        self.db.session.flush()
        return a

    # -- single-job route --------------------------------------------------

    def test_single_pending_updated(self):
        from myapp.database import ANALYSIS_PRIORITY_HIGH, Analysis
        u = self._user('rw', 'READ_WRITE')
        art, _ = self._artefact(u.id)
        a = self._analysis(art.id, 'PENDING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        r = self.client.post(f'/analysis/{a.uuid}/priority',
                             data={'priority': str(ANALYSIS_PRIORITY_HIGH)})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.db.session.get(Analysis, a.id).priority, ANALYSIS_PRIORITY_HIGH)

    def test_single_running_rejected(self):
        from myapp.database import Analysis
        u = self._user('rw', 'READ_WRITE')
        art, _ = self._artefact(u.id)
        a = self._analysis(art.id, 'RUNNING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        self.client.post(f'/analysis/{a.uuid}/priority', data={'priority': '10'})
        # RUNNING job's priority is unchanged.
        self.assertEqual(self.db.session.get(Analysis, a.id).priority, 0)

    def test_single_invalid_priority_rejected(self):
        from myapp.database import Analysis
        u = self._user('rw', 'READ_WRITE')
        art, _ = self._artefact(u.id)
        a = self._analysis(art.id, 'PENDING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        self.client.post(f'/analysis/{a.uuid}/priority', data={'priority': '999'})
        self.assertEqual(self.db.session.get(Analysis, a.id).priority, 0)

    def test_read_only_forbidden(self):
        u = self._user('ro', 'READ_ONLY')
        art, _ = self._artefact(u.id)
        a = self._analysis(art.id, 'PENDING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        r = self.client.post(f'/analysis/{a.uuid}/priority', data={'priority': '10'})
        self.assertEqual(r.status_code, 403)

    # -- artefact scope ----------------------------------------------------

    def test_artefact_scope_updates_only_pending(self):
        from myapp.database import ANALYSIS_PRIORITY_HIGH, Analysis
        u = self._user('rw', 'READ_WRITE')
        art, _ = self._artefact(u.id)
        pending = self._analysis(art.id, 'PENDING', priority=0)
        running = self._analysis(art.id, 'RUNNING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        r = self.client.post(f'/analysis/artefact/{art.uuid}/priority',
                             data={'priority': str(ANALYSIS_PRIORITY_HIGH)})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.db.session.get(Analysis, pending.id).priority, ANALYSIS_PRIORITY_HIGH)
        self.assertEqual(self.db.session.get(Analysis, running.id).priority, 0)

    def test_artefact_scope_excludes_private_derived(self):
        from myapp.database import ANALYSIS_PRIORITY_HIGH, Analysis
        owner = self._user('owner', 'READ_WRITE')
        other = self._user('other', 'READ_WRITE')
        # Public root owned by `owner`; a private derived child also owned by owner.
        root, item = self._artefact(owner.id, label='root')
        derived, _ = self._artefact(owner.id, label='secret', is_private=True,
                                    item=item, parent_id=root.id)
        a_root = self._analysis(root.id, 'PENDING', priority=0)
        a_derived = self._analysis(derived.id, 'PENDING', priority=0)
        self.db.session.commit()
        # `other` can view the public root but not the private derived artefact.
        self._login(other.id)
        self.client.post(f'/analysis/artefact/{root.uuid}/priority',
                         data={'priority': str(ANALYSIS_PRIORITY_HIGH)})
        self.assertEqual(self.db.session.get(Analysis, a_root.id).priority, ANALYSIS_PRIORITY_HIGH)
        # The private derived artefact's job must be untouched.
        self.assertEqual(self.db.session.get(Analysis, a_derived.id).priority, 0)

    # -- item scope --------------------------------------------------------

    def test_item_scope_updates_pending(self):
        from myapp.database import ANALYSIS_PRIORITY_HIGH, Analysis
        u = self._user('rw', 'READ_WRITE')
        art, item = self._artefact(u.id)
        art2, _ = self._artefact(u.id, label='b', item=item)
        a1 = self._analysis(art.id, 'PENDING', priority=0)
        a2 = self._analysis(art2.id, 'PENDING', priority=0)
        self.db.session.commit()
        self._login(u.id)
        r = self.client.post(f'/analysis/item/{item.uuid}/priority',
                             data={'priority': str(ANALYSIS_PRIORITY_HIGH)})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.db.session.get(Analysis, a1.id).priority, ANALYSIS_PRIORITY_HIGH)
        self.assertEqual(self.db.session.get(Analysis, a2.id).priority, ANALYSIS_PRIORITY_HIGH)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
