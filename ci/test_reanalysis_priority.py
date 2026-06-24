"""
User-chosen re-analysis priority and the can_prioritise_analyses gate.

Covers can_raise_analysis_priority() (admin / staff / flag / plain), the
server-authoritative clamp in the analyse() route (an over-default Urgent POST
is clamped for ungated users but honoured for privileged ones; Low is always
honoured), and OIDC role sync setting/clearing the flag.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_reanalysis_priority -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-reanalysis-priority-secret-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class _Base(unittest.TestCase):
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

    def _user(self, name, permission='READ_WRITE', is_admin=False, can_prio=False):
        from myapp.database import User, UserPermission
        u = User(username=name, password_hash='x', is_admin=is_admin,
                 permission=getattr(UserPermission, permission),
                 can_prioritise_analyses=can_prio)
        self.db.session.add(u)
        self.db.session.flush()
        return u


class TestCanRaisePriority(_Base):
    def test_truth_table(self):
        admin = self._user('admin', is_admin=True)
        staff = self._user('staff', permission='STAFF')
        flagged = self._user('flagged', permission='READ_WRITE', can_prio=True)
        plain = self._user('plain', permission='READ_WRITE')
        self.assertTrue(admin.can_raise_analysis_priority())
        self.assertTrue(staff.can_raise_analysis_priority())
        self.assertTrue(flagged.can_raise_analysis_priority())
        self.assertFalse(plain.can_raise_analysis_priority())


class TestAnalyseRouteClamp(_Base):
    def _artefact(self, owner_id):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        item = Item(name='reanalyse-item', owner_id=owner_id)
        self.db.session.add(item)
        self.db.session.flush()
        art = Artefact(
            item_id=item.id, label='r', artefact_type=ArtefactType.SCP,
            original_filename='r.scp', storage_path='r.scp',
            storage_directory=StorageDirectory.UPLOADS, owner_id=owner_id)
        self.db.session.add(art)
        self.db.session.flush()
        return art

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def _queued_priorities(self, artefact_id):
        """Priorities of the queued (PENDING) non-CLEANUP analyses."""
        from arcology_shared.enums import AnalysisType
        from myapp.database import Analysis
        return [
            a.priority for a in Analysis.query.filter(
                Analysis.artefact_id == artefact_id,
                Analysis.analysis_type != AnalysisType.CLEANUP,
            ).all()
        ]

    def _post(self, art_uuid, priority):
        return self.client.post(
            f'/artefacts/{art_uuid}/analyse',
            data={'priority': str(priority), 'platform_id': '0'},
            follow_redirects=False)

    def test_ungated_urgent_clamped_to_default(self):
        from myapp.database import ANALYSIS_PRIORITY_HIGH, ANALYSIS_PRIORITY_URGENT
        u = self._user('plain', permission='READ_WRITE')
        art = self._artefact(u.id)
        self.db.session.commit()
        self._login(u.id)
        self._post(art.uuid, ANALYSIS_PRIORITY_URGENT)
        priorities = self._queued_priorities(art.id)
        self.assertTrue(priorities)
        self.assertTrue(all(p == ANALYSIS_PRIORITY_HIGH for p in priorities),
                        f'expected clamp to {ANALYSIS_PRIORITY_HIGH}, got {priorities}')

    def test_flagged_urgent_honoured(self):
        from myapp.database import ANALYSIS_PRIORITY_URGENT
        u = self._user('flagged', permission='READ_WRITE', can_prio=True)
        art = self._artefact(u.id)
        self.db.session.commit()
        self._login(u.id)
        self._post(art.uuid, ANALYSIS_PRIORITY_URGENT)
        priorities = self._queued_priorities(art.id)
        self.assertTrue(priorities)
        self.assertTrue(all(p == ANALYSIS_PRIORITY_URGENT for p in priorities),
                        f'expected {ANALYSIS_PRIORITY_URGENT}, got {priorities}')

    def test_low_always_honoured(self):
        from myapp.database import ANALYSIS_PRIORITY_LOW
        u = self._user('plain', permission='READ_WRITE')
        art = self._artefact(u.id)
        self.db.session.commit()
        self._login(u.id)
        self._post(art.uuid, ANALYSIS_PRIORITY_LOW)
        priorities = self._queued_priorities(art.id)
        self.assertTrue(priorities)
        self.assertTrue(all(p == ANALYSIS_PRIORITY_LOW for p in priorities),
                        f'expected {ANALYSIS_PRIORITY_LOW}, got {priorities}')


class TestOidcRoleSync(_Base):
    def _sync(self, user, roles):
        from myapp.blueprints.oidc_auth import _sync_permissions
        _sync_permissions(user, {'roles': roles})

    def test_role_sets_and_clears_flag(self):
        user = self._user('sso', permission='READ_WRITE')
        user.oidc_managed = True
        self.db.session.flush()
        # Role grants the flag (plus a permission role so the account is not demoted).
        self._sync(user, ['arcology-read-write', 'arcology-prioritise'])
        self.assertTrue(user.can_prioritise_analyses)
        # Removing the role clears it.
        self._sync(user, ['arcology-read-write'])
        self.assertFalse(user.can_prioritise_analyses)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
