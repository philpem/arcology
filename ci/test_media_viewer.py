"""
Tests for the generalised media player in the artefact viewer.

Covers:
  * handle_media_transcode building MediaFile rows (insert + scoped delete).
  * The viewer renders a <video>/<audio> player for transcoded media (output
    URL) and passthrough media (inline stream route), with codec/track metadata.
  * Content gates: a restricted owning artefact withholds the player (no media
    src leaked); an EXPLICIT artefact a user can bypass shows the explicit gate.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_media_viewer -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-media-viewer-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _user(db, username, *, is_admin=False):
    import bcrypt
    from myapp.database import User, UserPermission
    pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
    u = User(username=username, password_hash=pw, is_admin=is_admin,
             permission=UserPermission.READ_WRITE, can_use_api=True)
    db.session.add(u)
    db.session.commit()
    return u


class TestMediaViewer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ArtefactRestriction,
            ExtractedFile,
            FilesystemType,
            Item,
            MediaFile,
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
        cls.app.storage = LocalStorage(
            uploads_dir=Path(cls._tmp.name) / 'uploads', outputs_dir=outputs)
        cls.app.config['OUTPUT_FOLDER'] = str(outputs)

        with cls.app.app_context():
            db.create_all()
            owner = _user(db, 'media-owner')
            cls.viewer_id = _user(db, 'media-viewer').id
            cls.admin_id = _user(db, 'media-admin', is_admin=True).id

            item = Item(name='media-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()

            def _art(label, atype, fname, **kw):
                a = Artefact(item_id=item.id, label=label, artefact_type=atype,
                             original_filename=fname, storage_path=f'uploads/{fname}',
                             owner_id=owner.id, **kw)
                db.session.add(a)
                db.session.flush()
                return a

            # A clean container (e.g. a disc) holding extracted media.
            disc = _art('disc', ArtefactType.RAW_SECTOR, 'disc.adf')
            part = Partition(artefact_id=disc.id, partition_index=0,
                             filesystem=FilesystemType.UNKNOWN)
            db.session.add(part)
            db.session.flush()

            # Transcoded video (non-native AVI → MP4 output).
            db.session.add(MediaFile(
                artefact_id=disc.id, file_path='Movies/clip.avi', media_kind='video',
                container_format='avi', video_codec='mpeg4', width=320, height=240,
                frame_rate=25.0, audio_codec='mp3', sample_rate=44100, channels=2,
                has_audio=True, duration_seconds=12.0,
                mp4_output_path=f'media-item/{disc.uuid}_disc/clip.mp4',
                poster_path=f'media-item/{disc.uuid}_disc/clip_poster.jpg'))

            # Passthrough audio (native MP3 — played from the extracted bytes).
            ef = ExtractedFile(partition_id=part.id, path='Music/song.mp3',
                               filename='song.mp3', extension='mp3')
            db.session.add(ef)
            db.session.add(MediaFile(
                artefact_id=disc.id, file_path='Music/song.mp3', media_kind='audio',
                container_format='mp3', audio_codec='mp3', sample_rate=44100,
                channels=2, has_audio=True, duration_seconds=180.0,
                mp4_output_path=None, poster_path=None))

            # EXPLICIT artefact carrying a transcoded movie.
            expl = _art('nsfw', ArtefactType.RAW_SECTOR, 'nsfw.adf')
            db.session.add(ArtefactRestriction(
                artefact_id=expl.id, restriction_type=RestrictionType.EXPLICIT, reason='t'))
            db.session.add(MediaFile(
                artefact_id=expl.id, file_path='Clips/nsfw.avi', media_kind='video',
                container_format='avi', video_codec='mpeg4', width=320, height=240,
                has_audio=False,
                mp4_output_path=f'media-item/{expl.uuid}_nsfw/nsfw.mp4',
                poster_path=f'media-item/{expl.uuid}_nsfw/nsfw_poster.jpg'))

            # COPYRIGHT-restricted artefact with a transcoded movie.
            restr = _art('copyr', ArtefactType.RAW_SECTOR, 'c.adf')
            db.session.add(ArtefactRestriction(
                artefact_id=restr.id, restriction_type=RestrictionType.COPYRIGHT, reason='t'))
            db.session.add(MediaFile(
                artefact_id=restr.id, file_path='Vid/secret.avi', media_kind='video',
                container_format='avi', video_codec='mpeg4',
                mp4_output_path=f'media-item/{restr.uuid}_copyr/secret.mp4',
                poster_path=None))

            # A passthrough (native) extracted media file on the EXPLICIT
            # artefact, used to exercise the inline stream route's restriction
            # gating (bypass user must be able to play it without confirm_bypass).
            epart = Partition(artefact_id=expl.id, partition_index=0,
                              filesystem=FilesystemType.UNKNOWN)
            db.session.add(epart)
            db.session.flush()
            eef = ExtractedFile(partition_id=epart.id, path='Clips/native.mp3',
                                filename='native.mp3', extension='mp3')
            db.session.add(eef)
            db.session.flush()

            db.session.commit()

            cls.item_url = item.url_id
            cls.disc_slug = disc.url_slug
            cls.expl_slug = expl.url_slug
            cls.restr_slug = restr.url_slug
            cls.disc_mp4 = f'media-item/{disc.uuid}_disc/clip.mp4'
            cls.explicit_native_uuid = eef.uuid

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _viewer(self, slug, **q):
        qs = '&'.join(f'{k}={v}' for k, v in q.items())
        url = f'/items/{self.item_url}/artefacts/{slug}/viewer'
        if qs:
            url += '?' + qs
        return self.client.get(url)

    def test_transcoded_video_player(self):
        self._login(self.admin_id)
        r = self._viewer(self.disc_slug, file='Movies/clip.avi')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('<video', html)
        # src points at the transcoded MP4 output route.
        self.assertIn('clip.mp4', html)
        # ffprobe metadata is surfaced.
        self.assertIn('mpeg4', html)

    def test_passthrough_audio_streams_original(self):
        self._login(self.admin_id)
        r = self._viewer(self.disc_slug, file='Music/song.mp3')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('<audio', html)
        # Passthrough plays via the inline stream route, not an output file.
        self.assertIn('/stream', html)
        self.assertIn('Original file', html)

    def test_media_thumbnail_in_grid(self):
        self._login(self.admin_id)
        r = self._viewer(self.disc_slug)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        # Both media files appear as thumbnails linking to their player.
        self.assertIn('file=Movies/clip.avi', html)
        self.assertIn('file=Music/song.mp3', html)

    def test_explicit_media_gate_for_bypass_user(self):
        self._login(self.admin_id)  # admin bypasses EXPLICIT
        r = self._viewer(self.expl_slug, file='Clips/nsfw.avi')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('explicit-gate', html)
        self.assertIn('<video', html)

    def test_restricted_media_withholds_player(self):
        self._login(self.viewer_id)  # no bypass for COPYRIGHT
        r = self._viewer(self.restr_slug, file='Vid/secret.avi')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        # The transcoded MP4 URL must not be emitted to a non-bypass user.
        self.assertNotIn('secret.mp4', html)

    def test_stream_route_bypass_user_not_redirected(self):
        # A bypass-capable user (admin bypasses EXPLICIT) must reach the inline
        # stream without the confirm_bypass download-override — otherwise the
        # <audio>/<video> fetch gets a 302-to-HTML and playback fails.  The file
        # isn't on disk in this test, so a passed restriction check surfaces as
        # 404 (serve returns None), NOT a 302 redirect to the artefact page.
        self._login(self.admin_id)
        r = self.client.get(f'/files/{self.explicit_native_uuid}/stream')
        self.assertEqual(r.status_code, 404)

    def test_stream_route_blocks_non_bypass_user(self):
        # A user who cannot bypass the EXPLICIT restriction is still hard-blocked
        # (redirected) by the stream route.
        self._login(self.viewer_id)
        r = self.client.get(f'/files/{self.explicit_native_uuid}/stream')
        self.assertEqual(r.status_code, 302)


class TestHandleMediaTranscodeIndex(unittest.TestCase):
    """handle_media_transcode inserts/replaces MediaFile rows."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db
        self.app = create_app()
        self.db = db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _artefact(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item
        it = Item(name='t')
        self.db.session.add(it)
        self.db.session.flush()
        a = Artefact(item_id=it.id, label='d', slug='d', original_filename='d.adf',
                     storage_path='uploads/d.adf', artefact_type=ArtefactType.RAW_SECTOR,
                     sha256='a' * 64, file_size=1)
        self.db.session.add(a)
        self.db.session.flush()
        return a

    def _analysis(self, art, details):
        import json as _json
        from myapp.database import Analysis, AnalysisStatus, AnalysisType
        an = Analysis(artefact_id=art.id, analysis_type=AnalysisType.MEDIA_TRANSCODE,
                      status=AnalysisStatus.COMPLETED, success=True)
        an.details = _json.dumps(details)
        self.db.session.add(an)
        self.db.session.flush()
        return an

    def test_inserts_rows(self):
        from myapp.database import MediaFile
        from myapp.services.search_index import handle_media_transcode
        art = self._artefact()
        details = {'transcoded': [
            {'file_path': 'a/x.avi', 'media_kind': 'video', 'video_codec': 'mpeg4',
             'mp4_output_path': 'o/x.mp4', 'poster_path': 'o/x.jpg'},
            {'file_path': 'a/y.mp3', 'media_kind': 'audio', 'audio_codec': 'mp3',
             'mp4_output_path': None, 'poster_path': None},
        ]}
        an = self._analysis(art, details)
        handle_media_transcode(an, details)
        self.db.session.commit()
        rows = {r.file_path: r for r in MediaFile.query.all()}
        self.assertEqual(set(rows), {'a/x.avi', 'a/y.mp3'})
        self.assertEqual(rows['a/x.avi'].mp4_output_path, 'o/x.mp4')
        self.assertIsNone(rows['a/y.mp3'].mp4_output_path)

    def test_scoped_delete_by_prefix(self):
        from myapp.database import MediaFile
        from myapp.services.search_index import handle_media_transcode
        art = self._artefact()
        # Seed a row under a different prefix that must survive.
        self.db.session.add(MediaFile(artefact_id=art.id, file_path='other/keep.avi',
                                      media_kind='video'))
        self.db.session.flush()
        details = {'path_prefix': 'arch/foo.zip', 'transcoded': [
            {'file_path': 'arch/foo.zip/new.avi', 'media_kind': 'video',
             'mp4_output_path': 'o/new.mp4'},
        ]}
        an = self._analysis(art, details)
        handle_media_transcode(an, details)
        self.db.session.commit()
        paths = {r.file_path for r in MediaFile.query.all()}
        self.assertIn('other/keep.avi', paths)
        self.assertIn('arch/foo.zip/new.avi', paths)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
