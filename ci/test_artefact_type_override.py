"""
Artefact type override / revert-to-autodetect tests.

Covers the edit route's handling of the artefact_type field:

  - Selecting a concrete type sets type_overridden=True.
  - Selecting "-- Auto-detect --" (value 'auto') re-runs detect_artefact_type()
    on the original filename and clears type_overridden.
  - The GET form pre-selects 'auto' when the type was not overridden, and the
    concrete type when it was.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_artefact_type_override -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-type-override-test-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _make_user(db, username):
    import bcrypt
    from myapp.database import User, UserPermission
    pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
    user = User(username=username, password_hash=pw,
                permission=UserPermission.READ_WRITE)
    db.session.add(user)
    db.session.flush()
    return user


_seq = [0]


def _make_artefact(db, artefact_type, *, filename='disk.scp', overridden=False):
    from myapp.database import Artefact, Item, Platform
    _seq[0] += 1
    n = _seq[0]
    platform = Platform(name=f'Override Platform {n}')
    db.session.add(platform)
    db.session.flush()
    item = Item(name=f'Override Item {n}', platform_id=platform.id)
    db.session.add(item)
    db.session.flush()
    artefact = Artefact(
        item_id=item.id,
        label=filename,
        artefact_type=artefact_type,
        type_overridden=overridden,
        original_filename=filename,
        storage_path=f'uploads/{filename}',
    )
    db.session.add(artefact)
    db.session.flush()
    return artefact


class TestEditTypeOverride(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            user = _make_user(db, 'override-editor')
            cls.user_id = user.id
            db.session.commit()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True

    def _post_edit(self, artefact, type_value):
        return self.client.post(
            f'/artefacts/{artefact.uuid}/edit',
            data={
                'label': artefact.label,
                'artefact_type': type_value,
                'description': '',
                'tags': '',
            },
            follow_redirects=False,
        )

    def test_setting_concrete_type_marks_overridden(self):
        from arcology_shared.enums import ArtefactType
        with self.app.app_context():
            art = _make_artefact(self.db, ArtefactType.SCP, filename='disk.scp')
            self.db.session.commit()
            uuid = art.uuid
            artobj = self.db.session.get(type(art), art.id)
            resp = self._post_edit(artobj, ArtefactType.IMD.value)
            self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            from myapp.database import Artefact
            art = Artefact.query.filter_by(uuid=uuid).one()
            self.assertEqual(art.artefact_type, ArtefactType.IMD)
            self.assertTrue(art.type_overridden)

    def test_auto_reverts_to_detected_type_and_clears_flag(self):
        from arcology_shared.enums import ArtefactType
        # Filename is .scp but type was manually overridden to IMD.
        with self.app.app_context():
            art = _make_artefact(self.db, ArtefactType.IMD,
                                 filename='disk.scp', overridden=True)
            self.db.session.commit()
            uuid = art.uuid
            artobj = self.db.session.get(type(art), art.id)
            resp = self._post_edit(artobj, 'auto')
            self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            from myapp.database import Artefact
            art = Artefact.query.filter_by(uuid=uuid).one()
            # detect_artefact_type('disk.scp') -> SCP
            self.assertEqual(art.artefact_type, ArtefactType.SCP)
            self.assertFalse(art.type_overridden)

    def test_get_form_preselects_auto_when_not_overridden(self):
        from arcology_shared.enums import ArtefactType
        with self.app.app_context():
            art = _make_artefact(self.db, ArtefactType.SCP,
                                 filename='disk2.scp', overridden=False)
            self.db.session.commit()
            uuid = art.uuid

        resp = self.client.get(f'/artefacts/{uuid}/edit')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # The auto-detect option should be present and selected.
        self.assertIn('-- Auto-detect --', body)
        self.assertIn('<option selected value="auto">', body)

    def test_get_form_preselects_concrete_type_when_overridden(self):
        from arcology_shared.enums import ArtefactType
        with self.app.app_context():
            art = _make_artefact(self.db, ArtefactType.IMD,
                                 filename='disk3.scp', overridden=True)
            self.db.session.commit()
            uuid = art.uuid

        resp = self.client.get(f'/artefacts/{uuid}/edit')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn(f'<option selected value="{ArtefactType.IMD.value}">', body)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
