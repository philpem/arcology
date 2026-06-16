"""
Tests for Acorn Replay / ARMovie → MP4 transcoding (REPLAY_TRANSCODE).

Covers three layers without needing the real scotch/ffmpeg binaries:

  1. The worker tool wrapper ``transcode_armovie_to_mp4`` — subprocess calls are
     mocked; the mock creates the expected output files so the existence checks
     fire as they would in production.
  2. The web search-index handler ``handle_replay_transcode`` — updates the
     existing ReplayMovie row (created by REPLAY_PROCESS) with the MP4/poster
     paths, and never inserts/deletes.
  3. The viewer detail helper ``_viewer_replay_detail`` — turns those stored
     paths into served URLs (mp4_url / poster_url).

Run:
    python -m unittest ci.test_replay_transcode -v
"""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')

from worker.arcworker.tools.replay_transcode import (
    transcode_armovie_to_audio,
    transcode_armovie_to_mp4,
)


def _make_fake_run(*, decode_ok=True, make_wav=True):
    """Build a run_tool_with_output stand-in that creates expected output files.

    Inspects the command to decide which file to create:
      - replay-transcode: writes --output (raw) and (if make_wav) --audio-output
      - ffmpeg: writes the final positional argument (mp4 or poster)
    Returns (CompletedProcess, {}) with returncode 0, except a failed decode
    returns returncode 1 and writes nothing.
    """
    def _run(cmd, timeout=None, cwd=None):
        if cmd[0] == 'replay-transcode':
            if not decode_ok:
                return subprocess.CompletedProcess(cmd, 1, b'', b'unsupported codec'), {}
            out = Path(cmd[cmd.index('--output') + 1])
            out.write_bytes(b'\x00' * 16)
            if make_wav:
                Path(cmd[cmd.index('--audio-output') + 1]).write_bytes(b'RIFFxxxx')
            return subprocess.CompletedProcess(cmd, 0, b'', b''), {}
        # ffmpeg (mux or poster) — last arg is the output file
        Path(cmd[-1]).write_bytes(b'\x00' * 8)
        return subprocess.CompletedProcess(cmd, 0, b'', b''), {}

    return _run


class TestTranscodeTool(unittest.TestCase):
    def test_success_with_audio_and_poster(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'movie.rpl'
            inp.write_bytes(b'ARMovie')
            mp4 = work / 'out.mp4'
            poster = work / 'out.jpg'
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_make_fake_run()):
                res = transcode_armovie_to_mp4(
                    inp, mp4, width=320, height=256, frame_rate=12.5,
                    work_dir=work, poster_path=poster,
                )
            self.assertTrue(res['success'])
            self.assertEqual(res['output_type'], 'mp4')
            self.assertTrue(res['has_audio'])
            self.assertEqual(res['poster_path'], str(poster))
            self.assertTrue(mp4.exists() and poster.exists())

    def test_success_without_audio(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'movie.rpl'
            inp.write_bytes(b'ARMovie')
            mp4 = work / 'out.mp4'
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_make_fake_run(make_wav=False)):
                res = transcode_armovie_to_mp4(
                    inp, mp4, width=160, height=128, frame_rate=None,
                    work_dir=work,
                )
            self.assertTrue(res['success'])
            self.assertFalse(res['has_audio'])
            self.assertIsNone(res['poster_path'])
            # Falls back to the default frame rate when the header had none.
            self.assertEqual(res['frame_rate'], 25.0)

    def test_missing_dimensions_fails_before_running(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output') as m:
                res = transcode_armovie_to_mp4(
                    work / 'm.rpl', work / 'o.mp4', width=None, height=256,
                    frame_rate=10, work_dir=work,
                )
            self.assertFalse(res['success'])
            self.assertEqual(res['stage'], 'decode')
            m.assert_not_called()

    def test_decode_failure_reports_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'movie.rpl'
            inp.write_bytes(b'ARMovie')
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_make_fake_run(decode_ok=False)):
                res = transcode_armovie_to_mp4(
                    inp, work / 'o.mp4', width=320, height=256, frame_rate=25,
                    work_dir=work,
                )
            self.assertFalse(res['success'])
            self.assertEqual(res['stage'], 'decode')

    def test_modules_dir_passed_through(self):
        seen = {}

        def _capture(cmd, timeout=None, cwd=None):
            if cmd[0] == 'replay-transcode':
                seen['cmd'] = cmd
            return _make_fake_run()(cmd, timeout, cwd)

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'movie.rpl'
            inp.write_bytes(b'ARMovie')
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_capture):
                transcode_armovie_to_mp4(
                    inp, work / 'o.mp4', width=320, height=256, frame_rate=25,
                    work_dir=work, modules_dir='/srv/replay-modules',
                )
        self.assertIn('--modules-dir', seen['cmd'])
        self.assertIn('/srv/replay-modules', seen['cmd'])

    def test_mux_input_is_rgb24_not_output_pixfmt(self):
        """The rawvideo INPUT must be rgb24 (replay-transcode's invariant output);
        it must never pick up the libx264 OUTPUT -pix_fmt (that reintroduces red)."""
        captured = {}

        def _run(cmd, timeout=None, cwd=None):
            if cmd[0] == 'replay-transcode':
                Path(cmd[cmd.index('--output') + 1]).write_bytes(b'\x00' * 16)
                # recipe carries BOTH tokens; the parser must not be fooled
                return (subprocess.CompletedProcess(cmd, 0, b'', b''),
                        {'stderr': 'ffmpeg -f rawvideo -pixel_format rgb24 -i - '
                                   '-c:v libx264 -pix_fmt yuv420p out.mp4'})
            if '-f' in cmd and 'rawvideo' in cmd:   # the mux call
                captured['mux'] = cmd
            Path(cmd[-1]).write_bytes(b'\x00' * 8)
            return subprocess.CompletedProcess(cmd, 0, b'', b''), {}

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'movie.rpl'
            inp.write_bytes(b'ARMovie')
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_run):
                res = transcode_armovie_to_mp4(
                    inp, work / 'out.mp4', width=160, height=128, frame_rate=25,
                    work_dir=work,
                )
            self.assertTrue(res['success'])
        mux = captured['mux']
        # The input pixel format (the one before '-i') is rgb24, not yuv420p.
        self.assertEqual(mux[mux.index('-pixel_format') + 1], 'rgb24')


class TestAudioOnlyTranscode(unittest.TestCase):
    def test_sound_only_produces_audio(self):
        def _run(cmd, timeout=None, cwd=None):
            if cmd[0] == 'replay-transcode':
                Path(cmd[cmd.index('--audio-output') + 1]).write_bytes(b'RIFFxxxx')
                return subprocess.CompletedProcess(cmd, 0, b'', b''), {}
            Path(cmd[-1]).write_bytes(b'\x00' * 8)   # ffmpeg m4a output
            return subprocess.CompletedProcess(cmd, 0, b'', b''), {}

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'sound.rpl'
            inp.write_bytes(b'ARMovie')
            out = work / 'sound.m4a'
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_run):
                res = transcode_armovie_to_audio(inp, out, work_dir=work)
            self.assertTrue(res['success'])
            self.assertTrue(res['audio_only'])
            self.assertTrue(res['has_audio'])
            self.assertEqual(res['output_type'], 'm4a')
            self.assertTrue(out.exists())

    def test_sound_only_no_audio_fails(self):
        def _run(cmd, timeout=None, cwd=None):
            # replay-transcode writes no WAV (e.g. nothing decodable)
            return subprocess.CompletedProcess(cmd, 0, b'', b'no audio'), {}

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            inp = work / 'sound.rpl'
            inp.write_bytes(b'ARMovie')
            with patch('worker.arcworker.tools.replay_transcode.run_tool_with_output',
                       side_effect=_run):
                res = transcode_armovie_to_audio(inp, work / 'o.m4a', work_dir=work)
        self.assertFalse(res['success'])
        self.assertEqual(res['stage'], 'decode')


class TestPosterSprite(unittest.TestCase):
    """Embedded ARMovie poster-sprite extraction (header lines 19/20)."""

    def test_no_poster_sprite_returns_none(self):
        # sprite_size 0 → no poster; returns cleanly without needing spritefile.
        from worker.arcworker.tools.images_acorn import convert_replay_poster_sprite
        with tempfile.TemporaryDirectory() as td:
            res = convert_replay_poster_sprite(b'ARMovie' + b'\x00' * 100, 8, 0, Path(td) / 'p.png')
        self.assertFalse(res['success'])
        self.assertIsNone(res['poster_path'])
        self.assertIn('No embedded poster sprite', res['error'])

    def test_out_of_bounds_extent_rejected(self):
        from worker.arcworker.tools.images_acorn import convert_replay_poster_sprite
        with tempfile.TemporaryDirectory() as td:
            res = convert_replay_poster_sprite(b'\x00' * 32, 16, 64, Path(td) / 'p.png')
        self.assertFalse(res['success'])
        self.assertIn('exceeds file size', res['error'])

    def test_save_replay_poster_saves_and_returns_path(self):
        """_save_replay_poster runs the extractor and stores the PNG output."""
        from unittest.mock import MagicMock
        from worker.arcworker.analyses import metadata

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            worker = MagicMock()
            worker.save_output_file.return_value = 'item/art/abc_0_poster.png'

            def _fake_convert(data, off, size, out_path):
                Path(out_path).write_bytes(b'\x89PNG')
                return {'success': True, 'poster_path': str(out_path)}

            with patch.object(metadata, 'convert_replay_poster_sprite', side_effect=_fake_convert):
                saved = metadata._save_replay_poster(
                    worker, b'\x00' * 4096,
                    {'sprite_offset': 100, 'sprite_size': 200},
                    work, 'item/art', 'abc_0',
                )
            self.assertEqual(saved, 'item/art/abc_0_poster.png')
            worker.save_output_file.assert_called_once()

    def test_save_replay_poster_skips_when_no_sprite(self):
        from unittest.mock import MagicMock
        from worker.arcworker.analyses import metadata
        worker = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(metadata, 'convert_replay_poster_sprite') as conv:
                saved = metadata._save_replay_poster(
                    worker, b'\x00' * 16, {'sprite_size': 0}, Path(td), 'sub', 'base',
                )
        self.assertIsNone(saved)
        conv.assert_not_called()
        worker.save_output_file.assert_not_called()


class TestSearchIndexAndViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls._db = _db
        with cls.app.app_context():
            _db.create_all()

    def _fixture(self):
        """Create an item + artefact + one ReplayMovie row (not yet transcoded)."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, ReplayMovie, StorageDirectory
        _db = self._db
        item = Item(name='Replay Sampler')
        _db.session.add(item)
        _db.session.flush()
        art = Artefact(
            item_id=item.id,
            label='Disc',
            artefact_type=ArtefactType.HFE,
            original_filename='disc.hfe',
            storage_path='disc.hfe',
            storage_directory=StorageDirectory.UPLOADS,
            md5='f' * 32,
            sha256='f' * 64,
        )
        _db.session.add(art)
        _db.session.flush()
        mov = ReplayMovie(
            artefact_id=art.id,
            file_path='Movies/Demo',
            title='Demo',
            video_format=1,
            video_label='1K',
            width=320,
            height=256,
        )
        _db.session.add(mov)
        _db.session.flush()
        return art, mov

    def test_handle_replay_transcode_updates_row(self):
        from myapp.database import ReplayMovie
        from myapp.services.search_index import handle_replay_transcode
        with self.app.app_context():
            art, mov = self._fixture()
            analysis = types.SimpleNamespace(artefact_id=art.id)
            details = {'transcoded': [{
                'file_path': 'Movies/Demo',
                'mp4_output_path': 'item_x/art_y/abc.mp4',
                'poster_path': 'item_x/art_y/abc.jpg',
            }]}
            handle_replay_transcode(analysis, details)
            self._db.session.flush()
            self._db.session.expire_all()
            refreshed = self._db.session.get(ReplayMovie, mov.id)
            self.assertEqual(refreshed.mp4_output_path, 'item_x/art_y/abc.mp4')
            self.assertEqual(refreshed.poster_path, 'item_x/art_y/abc.jpg')
            self._db.session.rollback()

    def test_handle_replay_transcode_skips_unknown_file(self):
        from myapp.database import ReplayMovie
        from myapp.services.search_index import handle_replay_transcode
        with self.app.app_context():
            art, mov = self._fixture()
            analysis = types.SimpleNamespace(artefact_id=art.id)
            handle_replay_transcode(analysis, {'transcoded': [{
                'file_path': 'Movies/DoesNotExist',
                'mp4_output_path': 'x.mp4',
            }]})
            self._db.session.flush()
            self._db.session.expire_all()
            self.assertIsNone(self._db.session.get(ReplayMovie, mov.id).mp4_output_path)
            self._db.session.rollback()

    def test_viewer_detail_builds_urls(self):
        from myapp.blueprints.artefacts import _viewer_replay_detail
        with self.app.app_context():
            art, mov = self._fixture()
            mov.mp4_output_path = 'item_x/art_y/abc.mp4'
            mov.poster_path = 'item_x/art_y/abc.jpg'
            self._db.session.flush()
            with self.app.test_request_context():
                detail = _viewer_replay_detail('Movies/Demo', [art.id])
            self.assertIsNotNone(detail['mp4_url'])
            self.assertIn('abc.mp4', detail['mp4_url'])
            self.assertIn('abc.jpg', detail['poster_url'])
            self._db.session.rollback()

    def test_viewer_detail_links_original_file(self):
        """original_url points at the extracted ARMovie file's download route."""
        from myapp.blueprints.artefacts import _viewer_replay_detail
        from myapp.database import ExtractedFile, FilesystemType, Partition
        with self.app.app_context():
            art, mov = self._fixture()
            part = Partition(
                artefact_id=art.id, partition_index=0,
                filesystem=FilesystemType.UNKNOWN,
            )
            self._db.session.add(part)
            self._db.session.flush()
            ef = ExtractedFile(
                partition_id=part.id,
                path='Movies/Demo',
                filename='Demo',
                risc_os_filetype='ae7',
            )
            self._db.session.add(ef)
            self._db.session.flush()
            with self.app.test_request_context():
                detail = _viewer_replay_detail('Movies/Demo', [art.id])
            self.assertIsNotNone(detail['original_url'])
            self.assertIn(ef.uuid, detail['original_url'])
            self._db.session.rollback()

    def test_viewer_detail_no_transcode_yet(self):
        from myapp.blueprints.artefacts import _viewer_replay_detail
        with self.app.app_context():
            art, mov = self._fixture()
            with self.app.test_request_context():
                detail = _viewer_replay_detail('Movies/Demo', [art.id])
            self.assertIsNone(detail['mp4_url'])
            self.assertIsNone(detail['poster_url'])
            self._db.session.rollback()

    def test_replay_groups_are_grid_groups(self):
        """_viewer_replay_groups yields is_replay groups keyed by file path so
        they interleave with the converted-output groups."""
        from myapp.blueprints.artefacts import _viewer_replay_groups
        with self.app.app_context():
            art, mov = self._fixture()
            mov.poster_path = 'item_x/art_y/abc.jpg'
            mov.mp4_output_path = 'item_x/art_y/abc.mp4'
            self._db.session.flush()
            with self.app.test_request_context():
                groups = _viewer_replay_groups([art.id], '')
            self.assertEqual(len(groups), 1)
            g = groups[0]
            self.assertTrue(g['is_replay'])
            # Keyed by the extracted-file path so it sorts/filters with the rest.
            self.assertEqual(g['source_file'], 'Movies/Demo')
            self.assertEqual(g['label'], 'Movies/Demo')
            self.assertEqual(g['outputs'], [])
            self.assertFalse(g['replay']['sound_only'])  # video_format=1
            self.assertTrue(g['replay']['has_mp4'])
            self.assertIn('abc.jpg', g['replay']['poster_url'])
            self._db.session.rollback()

    def test_replay_groups_sound_only_flag(self):
        """video_format == 0 marks the movie sound-only (drives the audio icon)."""
        from myapp.blueprints.artefacts import _viewer_replay_groups
        with self.app.app_context():
            art, mov = self._fixture()
            mov.video_format = 0
            self._db.session.flush()
            with self.app.test_request_context():
                groups = _viewer_replay_groups([art.id], '')
            self.assertTrue(groups[0]['replay']['sound_only'])
            self.assertFalse(groups[0]['replay']['has_mp4'])  # not transcoded
            self._db.session.rollback()

    def test_replay_groups_respect_path_filter(self):
        """The ?path= subdirectory filter prunes movies outside the prefix."""
        from myapp.blueprints.artefacts import _viewer_replay_groups
        with self.app.app_context():
            art, mov = self._fixture()
            with self.app.test_request_context():
                self.assertEqual(len(_viewer_replay_groups([art.id], 'Movies/')), 1)
                self.assertEqual(len(_viewer_replay_groups([art.id], 'Other/')), 0)
            self._db.session.rollback()


class TestHandlerOrdering(unittest.TestCase):
    def test_transcode_handler_after_process(self):
        """handle_replay_transcode must run after handle_replay_movies so the
        rows it updates already exist (both in live and rebuild paths)."""
        from arcology_shared.enums import AnalysisType
        from myapp.services.search_index import _HANDLER_MAP
        keys = list(_HANDLER_MAP.keys())
        self.assertIn(AnalysisType.REPLAY_TRANSCODE, keys)
        self.assertLess(
            keys.index(AnalysisType.REPLAY_PROCESS),
            keys.index(AnalysisType.REPLAY_TRANSCODE),
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
