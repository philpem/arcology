"""
Tests that download restrictions also gate analysis OUTPUTS, not just the
original artefact bytes.

A download restriction (MALWARE/PII/COPYRIGHT/LEGAL_HOLD/EXPLICIT/CORRUPTED)
blocks downloading an artefact's original bytes.  But analysis outputs
(visualisations, Sprite/Draw image renders, text conversions) are renderings of
that same content.  Before this fix, a user who could *view* a restricted
artefact but held no bypass could still read its outputs via:

  * GET /outputs/<path>            (web, serves images + text files)
  * GET /api/outputs/<path>        (REST API, user keys)
  * the inline text embedded in the converter viewer page

These tests prove each path now enforces can_download_despite_restrictions().

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_output_restrictions -v
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
os.environ.setdefault('SECRET_KEY', 'ci-output-restriction-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_SECRET = b'TOP-SECRET-RESTRICTED-OUTPUT-CONTENT'


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


class TestOutputRestrictionGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            ApiKeyPermission,
            Artefact,
            ArtefactRestriction,
            Item,
            RestrictionType,
        )
        from myapp.extensions import db
        from shared.enums import ArtefactType
        from shared.storage import LocalStorage

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db

        cls._tmp = tempfile.TemporaryDirectory()
        outputs = Path(cls._tmp.name) / 'outputs'
        cls.app.storage = LocalStorage(
            uploads_dir=Path(cls._tmp.name) / 'uploads', outputs_dir=outputs)
        # The web endpoint's local-file serving uses get_output_folder()
        # (config OUTPUT_FOLDER), independent of the storage object.
        cls.app.config['OUTPUT_FOLDER'] = str(outputs)

        with cls.app.app_context():
            db.create_all()
            owner, _ = _user(db, 'restr-owner')
            _viewer, cls.key_viewer = _user(db, 'restr-viewer', api_perm=ApiKeyPermission.READ_ONLY)
            cls.viewer_id = _viewer.id
            _admin, cls.key_admin = _user(db, 'restr-admin', is_admin=True,
                                          api_perm=ApiKeyPermission.READ_WRITE)
            cls.admin_id = _admin.id

            # Public item (so the viewer can SEE it) with a COPYRIGHT-restricted
            # artefact owned by someone else.
            item = Item(name='pub-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='restricted', artefact_type=ArtefactType.ACORN_TEXT,
                           original_filename='secret.txt', storage_path='uploads/secret.txt',
                           owner_id=owner.id)
            db.session.add(art)
            db.session.flush()
            db.session.add(ArtefactRestriction(
                artefact_id=art.id, restriction_type=RestrictionType.COPYRIGHT, reason='test'))

            # An on-disk text output and a recorded FORMAT_CONVERT analysis so
            # the viewer would try to render it inline.
            sub = outputs / 'pub-item' / f'{art.uuid}_restricted'
            sub.mkdir(parents=True, exist_ok=True)
            (sub / 'conv.txt').write_bytes(_SECRET)
            cls.output_path = f'pub-item/{art.uuid}_restricted/conv.txt'

            conv = Analysis(artefact_id=art.id, analysis_type=AnalysisType.FORMAT_CONVERT,
                            status=AnalysisStatus.COMPLETED, success=True)
            conv.details = json.dumps({'outputs': [
                {'type': 'text', 'name': 'conv.txt', 'filename': cls.output_path},
            ]})
            db.session.add(conv)

            # ── Mode 2 aggregate scenario ────────────────────────────────────
            # An UNRESTRICTED container artefact (ZIP) whose DERIVED child is
            # COPYRIGHT-restricted and has an image FORMAT_CONVERT output.  The
            # viewer aggregates the child's outputs; before the tidy-up the
            # child's image rendered as a broken thumbnail (403) instead of a
            # per-group restricted notice.
            container = Artefact(item_id=item.id, label='archive',
                                 artefact_type=ArtefactType.ZIP,
                                 original_filename='archive.zip',
                                 storage_path='uploads/archive.zip', owner_id=owner.id)
            db.session.add(container)
            db.session.flush()
            child = Artefact(item_id=item.id, label='child', artefact_type=ArtefactType.ACORN_SPRITE,
                             original_filename='pic.spr', storage_path='uploads/pic.spr',
                             owner_id=owner.id, parent_artefact_id=container.id)
            db.session.add(child)
            db.session.flush()
            db.session.add(ArtefactRestriction(
                artefact_id=child.id, restriction_type=RestrictionType.COPYRIGHT, reason='test'))
            cls.child_img_path = f'pub-item/{child.uuid}_child/pic.png'
            child_conv = Analysis(artefact_id=child.id, analysis_type=AnalysisType.FORMAT_CONVERT,
                                  status=AnalysisStatus.COMPLETED, success=True)
            child_conv.details = json.dumps({'outputs': [
                {'type': 'image', 'name': 'pic.png', 'source_file': 'pic.spr',
                 'filename': cls.child_img_path},
            ]})
            db.session.add(child_conv)

            db.session.commit()

            cls.art_uuid = art.uuid
            cls.item_url = item.url_id
            cls.art_slug = art.url_slug
            cls.container_slug = container.url_slug

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    # ---- web GET /outputs/<path> ----
    def test_web_output_blocked_for_non_bypass_viewer(self):
        self._login(self.viewer_id)
        r = self.client.get(f'/outputs/{self.output_path}')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_web_output_allowed_for_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/outputs/{self.output_path}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    # ---- REST API GET /api/outputs/<path> ----
    def test_api_output_blocked_for_non_bypass_key(self):
        r = self.client.get(f'/api/outputs/{self.output_path}',
                            headers={'X-API-Key': self.key_viewer})
        self.assertEqual(r.status_code, 403, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_api_output_allowed_for_admin_key(self):
        r = self.client.get(f'/api/outputs/{self.output_path}',
                            headers={'X-API-Key': self.key_admin})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    # ---- inline text in the converter viewer page ----
    def test_viewer_does_not_embed_restricted_text(self):
        self._login(self.viewer_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(_SECRET, r.data)

    def test_viewer_embeds_text_for_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(_SECRET, r.data)

    # ---- Mode 2 aggregate: restricted derived-artefact image output ----
    def test_mode2_viewer_withholds_restricted_child_image(self):
        # Viewing the UNRESTRICTED container as a non-bypass viewer must not
        # emit an <img> pointing at the restricted child's output (it would
        # 403 → broken thumbnail); instead a per-group notice is shown.
        self._login(self.viewer_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.container_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(self.child_img_path.encode(), r.data)
        self.assertIn(b'download restriction', r.data)

    def test_mode2_viewer_shows_child_image_for_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.container_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(self.child_img_path.encode(), r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
