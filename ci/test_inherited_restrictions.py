"""
Tests that a download restriction on a CONTAINER artefact is inherited by the
artefacts DERIVED from it (and their analysis outputs / extracted files).

A restriction on e.g. BADFILE.ZIP must protect the ISO extracted from it, that
ISO's rendered outputs, and the files extracted from that ISO — even when the
derived artefact carries no restriction of its own.  Bypass grants already
cascade *down* the derivation chain (Artefact.ancestor_ids); these tests prove
restrictions are now collected *up* the same chain
(Artefact.effective_restrictions) at every download/output gate.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_inherited_restrictions -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-inherited-restriction-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_SECRET = b'INHERITED-RESTRICTED-DERIVED-OUTPUT'


def _user(db, username, *, is_admin=False, api_perm=None):
    import bcrypt
    from myapp.database import ApiKey, User, UserPermission
    pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
    u = User(username=username, password_hash=pw, is_admin=is_admin,
             permission=UserPermission.READ_WRITE, can_use_api=True)
    db.session.add(u)
    db.session.flush()
    raw = None
    if api_perm is not None:
        key, raw = ApiKey.create(user_id=u.id, name=f'{username}-key', permission=api_perm)
        db.session.add(key)
    db.session.commit()
    return u, raw


class TestInheritedRestrictionGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            ApiKeyPermission,
            Artefact,
            ArtefactRestriction,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
            RestrictionType,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db

        cls._tmp = tempfile.TemporaryDirectory()
        outputs = Path(cls._tmp.name) / 'outputs'
        uploads = Path(cls._tmp.name) / 'uploads'
        cls.app.storage = LocalStorage(uploads_dir=uploads, outputs_dir=outputs)
        cls.app.config['OUTPUT_FOLDER'] = str(outputs)
        cls.app.config['UPLOAD_FOLDER'] = str(uploads)

        with cls.app.app_context():
            db.create_all()
            owner, _ = _user(db, 'inh-owner')
            viewer, cls.key_viewer = _user(db, 'inh-viewer', api_perm=ApiKeyPermission.READ_ONLY)
            cls.viewer_id = viewer.id
            admin, cls.key_admin = _user(db, 'inh-admin', is_admin=True,
                                         api_perm=ApiKeyPermission.READ_WRITE)
            cls.admin_id = admin.id

            item = Item(name='inh-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()

            # Restricted CONTAINER (the ZIP).
            container = Artefact(item_id=item.id, label='archive',
                                 artefact_type=ArtefactType.ZIP,
                                 original_filename='archive.zip',
                                 storage_path='uploads/archive.zip', owner_id=owner.id)
            db.session.add(container)
            db.session.flush()
            db.session.add(ArtefactRestriction(
                artefact_id=container.id, restriction_type=RestrictionType.COPYRIGHT,
                reason='container restricted'))

            # DERIVED child with NO restriction of its own.
            child = Artefact(item_id=item.id, label='child', artefact_type=ArtefactType.ACORN_SPRITE,
                             original_filename='pic.spr', storage_path='pic.spr',
                             owner_id=owner.id, parent_artefact_id=container.id)
            db.session.add(child)
            db.session.flush()
            cls.child_uuid = child.uuid

            # Put the child's upload bytes on disk so an allowed download serves.
            (uploads / 'pic.spr').parent.mkdir(parents=True, exist_ok=True)
            (uploads / 'pic.spr').write_bytes(_SECRET)

            # The child's rendered image output on disk.
            sub = outputs / 'inh-item' / f'{child.uuid}_child'
            sub.mkdir(parents=True, exist_ok=True)
            (sub / 'pic.png').write_bytes(_SECRET)
            cls.child_output = f'inh-item/{child.uuid}_child/pic.png'

            conv = Analysis(artefact_id=child.id, analysis_type=AnalysisType.FORMAT_CONVERT,
                            status=AnalysisStatus.COMPLETED, success=True)
            conv.details = json.dumps({'outputs': [
                {'type': 'image', 'name': 'pic.png', 'source_file': 'pic.spr',
                 'filename': cls.child_output},
            ]})
            db.session.add(conv)

            # An extracted file under the child (case c).
            part = Partition(artefact_id=child.id, partition_index=0, total_files=1,
                             filesystem=FilesystemType.UNKNOWN)
            db.session.add(part)
            db.session.flush()
            ef = ExtractedFile(partition_id=part.id, path='inner.txt', filename='inner.txt',
                               is_directory=False, file_size=len(_SECRET))
            db.session.add(ef)
            db.session.flush()
            cls.ef_uuid = ef.uuid

            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    # ---- model: effective_restrictions ----
    def test_effective_restrictions_property(self):
        from myapp.database import Artefact
        with self.app.app_context():
            child = Artefact.query.filter_by(uuid=self.child_uuid).first()
            rtypes = {r.restriction_type for r in child.effective_restrictions}
            # Child has no own restriction; the COPYRIGHT comes from the container.
            self.assertFalse(child.restrictions)
            self.assertEqual(len(rtypes), 1)
            self.assertTrue(any(r.restriction_type.name == 'COPYRIGHT'
                                for r in child.effective_restrictions))

    # ---- (b) derived artefact OUTPUTS ----
    def test_web_derived_output_blocked_for_viewer(self):
        self._login(self.viewer_id)
        r = self.client.get(f'/outputs/{self.child_output}')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_web_derived_output_allowed_for_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/outputs/{self.child_output}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    def test_api_derived_output_blocked_for_viewer_key(self):
        r = self.client.get(f'/api/outputs/{self.child_output}',
                            headers={'X-API-Key': self.key_viewer})
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_api_derived_output_allowed_for_admin_key(self):
        r = self.client.get(f'/api/outputs/{self.child_output}',
                            headers={'X-API-Key': self.key_admin})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    # ---- (a) derived artefact BYTES ----
    def test_api_derived_download_blocked_for_viewer_key(self):
        r = self.client.get(f'/api/artefacts/{self.child_uuid}/download',
                            headers={'X-API-Key': self.key_viewer})
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_api_derived_download_allowed_for_admin_key(self):
        r = self.client.get(f'/api/artefacts/{self.child_uuid}/download',
                            headers={'X-API-Key': self.key_admin})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    # ---- (c) extracted file of a derived artefact ----
    def test_api_derived_extracted_file_blocked_for_viewer_key(self):
        r = self.client.get(f'/api/files/{self.ef_uuid}/download',
                            headers={'X-API-Key': self.key_viewer})
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
