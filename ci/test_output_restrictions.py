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
_SECRET_TEXT = _SECRET.decode()


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
            Item,
            ReplayMovie,
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
                {'type': 'text', 'name': 'conv.txt', 'filename': cls.output_path,
                 'text': _SECRET_TEXT},
            ]})
            db.session.add(conv)
            db.session.flush()
            cls.analysis_uuid = conv.uuid

            # A transcoded Replay movie on the same restricted artefact: its
            # player/poster are renderings of restricted content, so the viewer
            # must not emit the MP4/poster URLs for a non-bypass user.
            cls.replay_mp4_path = f'pub-item/{art.uuid}_restricted/movie.mp4'
            cls.replay_poster_path = f'pub-item/{art.uuid}_restricted/movie_poster.png'
            db.session.add(ReplayMovie(
                artefact_id=art.id, file_path='Movies/Secret', title='Secret',
                video_format=1, width=320, height=256,
                mp4_output_path=cls.replay_mp4_path,
                poster_path=cls.replay_poster_path,
            ))

            # ── Explicit (NSFW) Replay scenario ──────────────────────────────
            # An EXPLICIT-restricted artefact carrying a transcoded movie: a
            # non-bypass user gets a hard block; a bypass user (admin) sees the
            # poster behind the blur/reveal consent gate.
            expl = Artefact(item_id=item.id, label='nsfw', artefact_type=ArtefactType.HFE,
                            original_filename='nsfw.hfe', storage_path='uploads/nsfw.hfe',
                            owner_id=owner.id)
            db.session.add(expl)
            db.session.flush()
            db.session.add(ArtefactRestriction(
                artefact_id=expl.id, restriction_type=RestrictionType.EXPLICIT, reason='test'))
            cls.explicit_poster_path = f'pub-item/{expl.uuid}_nsfw/clip_poster.png'
            db.session.add(ReplayMovie(
                artefact_id=expl.id, file_path='Clips/Nsfw', title='Nsfw',
                video_format=1, width=320, height=256,
                mp4_output_path=f'pub-item/{expl.uuid}_nsfw/clip.mp4',
                poster_path=cls.explicit_poster_path,
            ))

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
            cls.explicit_art_slug = expl.url_slug

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

    # ---- inline converted text in REST API analysis details ----
    def test_api_analysis_details_hidden_from_non_bypass_key(self):
        urls = (
            f'/api/artefacts/{self.art_uuid}/analysis',
            f'/api/analysis/{self.analysis_uuid}',
            f'/api/artefacts/{self.art_uuid}/analysis/tree',
            f'/api/artefacts/{self.art_uuid}/processing-tree',
            f'/api/artefacts/{self.art_uuid}/analysis/recursive',
        )
        for url in urls:
            with self.subTest(url=url):
                r = self.client.get(url, headers={'X-API-Key': self.key_viewer})
                self.assertEqual(r.status_code, 200, r.data)
                self.assertNotIn(_SECRET, r.data)

    def test_api_analysis_details_visible_to_bypass_key(self):
        urls = (
            f'/api/artefacts/{self.art_uuid}/analysis',
            f'/api/analysis/{self.analysis_uuid}',
            f'/api/artefacts/{self.art_uuid}/analysis/tree',
            f'/api/artefacts/{self.art_uuid}/processing-tree',
            f'/api/artefacts/{self.art_uuid}/analysis/recursive',
        )
        for url in urls:
            with self.subTest(url=url):
                r = self.client.get(url, headers={'X-API-Key': self.key_admin})
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

    # ---- Replay player/poster on a restricted artefact ----
    def test_viewer_withholds_restricted_replay_media(self):
        # Non-bypass viewer: the Replay player/poster (renderings of restricted
        # content) must not be emitted, the same way image outputs are hidden.
        self._login(self.viewer_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(self.replay_mp4_path.encode(), r.data)
        self.assertNotIn(self.replay_poster_path.encode(), r.data)
        self.assertIn(b'download restriction', r.data)

    def test_viewer_shows_replay_media_for_admin(self):
        self._login(self.admin_id)
        r = self.client.get(f'/items/{self.item_url}/artefacts/{self.art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(self.replay_poster_path.encode(), r.data)

    # ---- Explicit (NSFW) Replay media ----
    def test_explicit_replay_blurred_for_bypass_user(self):
        # Admin bypasses EXPLICIT: the poster is present but wrapped in the
        # blur/reveal consent gate (not shown outright).
        self._login(self.admin_id)
        r = self.client.get(
            f'/items/{self.item_url}/artefacts/{self.explicit_art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(self.explicit_poster_path.encode(), r.data)
        self.assertIn(b'explicit-content-blur', r.data)
        self.assertIn(b'revealExplicit', r.data)

    def test_explicit_replay_hidden_for_non_bypass_user(self):
        # Non-bypass viewer: EXPLICIT is a hard block — no poster, restriction notice.
        self._login(self.viewer_id)
        r = self.client.get(
            f'/items/{self.item_url}/artefacts/{self.explicit_art_slug}/viewer')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn(self.explicit_poster_path.encode(), r.data)
        self.assertIn(b'download restriction', r.data)


class TestExtractedOutputRestrictions(unittest.TestCase):
    """File-only restrictions gate converted bytes and HTML, not clean siblings."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            ApiKeyPermission,
            Artefact,
            ArtefactType,
            ExtractedFile,
            ExtractedFileRestriction,
            FilesystemType,
            Item,
            MediaFile,
            Partition,
            ReplayMovie,
            RestrictionType,
            UserArtefactBypass,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, PUBLIC_MODE=True)
        cls.client = cls.app.test_client()
        cls.db = db
        cls._tmp = tempfile.TemporaryDirectory()
        outputs = Path(cls._tmp.name) / 'outputs'
        cls.app.storage = LocalStorage(
            uploads_dir=Path(cls._tmp.name) / 'uploads', outputs_dir=outputs)
        cls.app.config['OUTPUT_FOLDER'] = str(outputs)
        with cls.app.app_context():
            db.create_all()
            owner, _ = _user(db, 'file-owner')
            viewer, cls.key_viewer = _user(db, 'file-viewer', api_perm=ApiKeyPermission.READ_ONLY)
            admin, cls.key_admin = _user(db, 'file-admin', is_admin=True,
                                          api_perm=ApiKeyPermission.READ_WRITE)
            grantee, cls.key_grantee = _user(db, 'file-grantee', api_perm=ApiKeyPermission.READ_ONLY)
            cls.viewer_id, cls.admin_id, cls.grantee_id = viewer.id, admin.id, grantee.id
            item = Item(name='file-restrictions', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='container', artefact_type=ArtefactType.ZIP,
                           original_filename='files.zip', storage_path='uploads/files.zip', owner_id=owner.id)
            clean_art = Artefact(item_id=item.id, label='unrelated', artefact_type=ArtefactType.ZIP,
                                 original_filename='other.zip', storage_path='uploads/other.zip', owner_id=owner.id)
            db.session.add_all([art, clean_art])
            db.session.flush()
            cls.art_id, cls.clean_art_id = art.id, clean_art.id
            cls.art_uuid = art.uuid
            cls.paths = {}
            entries = []
            p = Partition(artefact_id=art.id, filesystem=FilesystemType.UNKNOWN)
            clean_p = Partition(artefact_id=clean_art.id, filesystem=FilesystemType.UNKNOWN)
            db.session.add_all([p, clean_p])
            db.session.flush()
            parent = ExtractedFile(partition_id=p.id, path='locked.zip', filename='locked.zip')
            db.session.add(parent)
            db.session.flush()
            db.session.add(ExtractedFileRestriction(
                extracted_file_id=parent.id, restriction_type=RestrictionType.COPYRIGHT))
            for name, kind, rtype, parent_id in (
                ('secret.txt', 'text', RestrictionType.EXPLICIT, None),
                ('secret.png', 'image', RestrictionType.EXPLICIT, None),
                ('secret.svg', 'svg', RestrictionType.PII, None),
                ('locked.zip/inside.txt', 'text', None, parent.id),
                ('clean.txt', 'text', None, None),
            ):
                ef = ExtractedFile(partition_id=p.id, path=name, filename=name.split('/')[-1],
                                   parent_file_id=parent_id)
                db.session.add(ef)
                db.session.flush()
                if rtype:
                    db.session.add(ExtractedFileRestriction(
                        extracted_file_id=ef.id, restriction_type=rtype))
                output = f'files/{art.uuid}_container/{name.replace("/", "_")}'
                path = outputs / output
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_SECRET if name != 'clean.txt' else b'CLEAN-SIBLING-CONTENT')
                cls.paths[name] = output
                entry = {'type': kind, 'name': name, 'filename': output, 'source_file': name}
                if kind == 'text':
                    entry['text'] = _SECRET_TEXT if name != 'clean.txt' else 'CLEAN-SIBLING-CONTENT'
                if kind == 'svg':
                    alias = output + '.legacy.svg'
                    (outputs / alias).write_bytes(_SECRET)
                    entry['svg_filename'] = alias
                    cls.paths['svg-alias'] = alias
                entries.append(entry)
            conv = Analysis(artefact_id=art.id, analysis_type=AnalysisType.FORMAT_CONVERT,
                            status=AnalysisStatus.COMPLETED, success=True,
                            details=json.dumps({'outputs': entries}))
            db.session.add(conv)
            db.session.flush()
            cls.analysis_uuid = conv.uuid
            # The identical path in a different artefact must not taint the clean source.
            db.session.add(ExtractedFile(partition_id=clean_p.id, path='secret.txt', filename='secret.txt'))
            cls.unrelated_output = f'files/{clean_art.uuid}_unrelated/clean.txt'
            (outputs / cls.unrelated_output).parent.mkdir(parents=True, exist_ok=True)
            (outputs / cls.unrelated_output).write_bytes(b'UNRELATED-CLEAN-CONTENT')
            db.session.add(Analysis(
                artefact_id=clean_art.id, analysis_type=AnalysisType.FORMAT_CONVERT,
                status=AnalysisStatus.COMPLETED, success=True,
                details=json.dumps({'outputs': [{'type': 'text', 'filename': cls.unrelated_output,
                                                'source_file': 'secret.txt'}]})))
            cls.media_paths = []
            for model, name, prefix in ((ReplayMovie, 'clip.rpl', 'a'), (MediaFile, 'clip.avi', 'b')):
                ef = ExtractedFile(partition_id=p.id, path=name, filename=name)
                db.session.add(ef)
                db.session.flush()
                db.session.add(ExtractedFileRestriction(
                    extracted_file_id=ef.id, restriction_type=RestrictionType.EXPLICIT))
                movie = f'media/{prefix * 64}/1/movie.mp4'
                poster = f'media/{prefix * 64}/1/poster.png'
                for output in (movie, poster):
                    (outputs / output).parent.mkdir(parents=True, exist_ok=True)
                    (outputs / output).write_bytes(_SECRET)
                    cls.media_paths.append(output)
                db.session.add(model(artefact_id=art.id, file_path=name,
                                     mp4_output_path=movie, poster_path=poster))
            for rtype in (RestrictionType.EXPLICIT, RestrictionType.PII, RestrictionType.COPYRIGHT):
                db.session.add(UserArtefactBypass(user_id=grantee.id, artefact_id=art.id,
                                                 restriction_type=rtype))
            db.session.commit()
            cls.viewer_url = f'/items/{item.url_id}/artefacts/{art.url_slug}/viewer'

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def test_web_and_api_reject_file_only_restrictions_and_ancestor_restrictions(self):
        self._login(self.viewer_id)
        for name, output in self.paths.items():
            if name == 'clean.txt':
                continue
            for prefix in ('/outputs/', '/api/outputs/'):
                with self.subTest(name=name, endpoint=prefix):
                    r = self.client.get(prefix + output, headers={'X-API-Key': self.key_viewer})
                    self.assertEqual(r.status_code, 403, r.data)
                    self.assertNotIn(_SECRET, r.data)

    def test_viewer_withholds_text_image_svg_and_media_markup(self):
        self._login(self.viewer_id)
        for query in ('?thumb=0', '?thumb=1', '?file=secret.txt', '?file=clip.rpl', '?file=clip.avi'):
            with self.subTest(query=query):
                r = self.client.get(self.viewer_url + query)
                self.assertEqual(r.status_code, 200, r.data)
                self.assertNotIn(_SECRET, r.data)
                for name, output in self.paths.items():
                    if name != 'clean.txt':
                        self.assertNotIn(output.encode(), r.data)
                for output in self.media_paths:
                    self.assertNotIn(output.encode(), r.data)
        r = self.client.get(self.viewer_url + '?thumb=0')
        self.assertIn(b'CLEAN-SIBLING-CONTENT', r.data)

    def test_unrestricted_sibling_and_same_path_in_other_artefact_remain_accessible(self):
        self._login(self.viewer_id)
        for output in (self.paths['clean.txt'], self.unrelated_output):
            for prefix in ('/outputs/', '/api/outputs/'):
                r = self.client.get(prefix + output, headers={'X-API-Key': self.key_viewer})
                self.assertEqual(r.status_code, 200, r.data)

    def test_admin_and_per_artefact_bypass_can_read_outputs_and_viewer(self):
        for uid, key in ((self.admin_id, self.key_admin), (self.grantee_id, self.key_grantee)):
            self._login(uid)
            for output in list(self.paths.values()) + self.media_paths:
                for prefix in ('/outputs/', '/api/outputs/'):
                    with self.subTest(user=uid, output=output, endpoint=prefix):
                        r = self.client.get(prefix + output, headers={'X-API-Key': key})
                        self.assertEqual(r.status_code, 200, r.data)
            r = self.client.get(self.viewer_url + '?thumb=0')
            self.assertIn(_SECRET, r.data)
            self.assertIn(self.paths['secret.png'].encode(), r.data)
            self.assertIn(b'explicit-gate', r.data)

    def test_shared_media_outputs_do_not_bypass_file_restrictions(self):
        self._login(self.viewer_id)
        for output in self.media_paths:
            for prefix in ('/outputs/', '/api/outputs/'):
                r = self.client.get(prefix + output, headers={'X-API-Key': self.key_viewer})
                self.assertEqual(r.status_code, 403, r.data)

    def test_shared_media_remains_available_through_an_unrestricted_owner(self):
        from myapp.database import MediaFile
        movie, poster = self.media_paths[2:]
        with self.app.app_context():
            row = MediaFile(artefact_id=self.clean_art_id, file_path='secret.txt',
                            mp4_output_path=movie, poster_path=poster)
            self.db.session.add(row)
            self.db.session.commit()
            row_id = row.id
        try:
            self._login(self.viewer_id)
            r = self.client.get('/api/outputs/' + movie, headers={'X-API-Key': self.key_viewer})
            self.assertEqual(r.status_code, 200, r.data)
        finally:
            with self.app.app_context():
                self.db.session.delete(self.db.session.get(MediaFile, row_id))
                self.db.session.commit()

    def test_anonymous_and_worker_output_requests_are_blocked(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        r = self.client.get('/outputs/' + self.paths['secret.txt'])
        self.assertEqual(r.status_code, 403, r.data)
        r = self.client.get('/api/outputs/' + self.paths['secret.txt'],
                            headers={'X-API-Key': os.environ['WORKER_API_KEY']})
        self.assertEqual(r.status_code, 403, r.data)

    def test_restricted_outputs_never_generate_storage_presigned_urls(self):
        from unittest.mock import patch

        self._login(self.viewer_id)
        with patch.object(self.app.storage, 'presigned_url') as presign:
            for prefix in ('/outputs/', '/api/outputs/'):
                r = self.client.get(prefix + self.paths['secret.txt'],
                                    headers={'X-API-Key': self.key_viewer})
                self.assertEqual(r.status_code, 403, r.data)
            presign.assert_not_called()

    def test_analysis_details_cannot_leak_inline_restricted_text(self):
        self._login(self.viewer_id)
        for url in (
            f'/api/analysis/{self.analysis_uuid}', f'/analysis/{self.analysis_uuid}',
            f'/api/artefacts/{self.art_uuid}/analysis',
            f'/api/artefacts/{self.art_uuid}/analysis/tree',
            f'/api/artefacts/{self.art_uuid}/processing-tree',
            f'/api/artefacts/{self.art_uuid}/analysis/recursive',
        ):
            r = self.client.get(url, headers={'X-API-Key': self.key_viewer})
            self.assertEqual(r.status_code, 200, r.data)
            self.assertNotIn(_SECRET, r.data)
        r = self.client.get(f'/api/analysis/{self.analysis_uuid}',
                            headers={'X-API-Key': self.key_admin})
        self.assertIn(_SECRET, r.data)
        r = self.client.get(f'/api/analysis/{self.analysis_uuid}',
                            headers={'X-API-Key': os.environ['WORKER_API_KEY']})
        self.assertIn(_SECRET, r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
