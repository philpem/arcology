"""
Unit tests for the MEDIA_TRANSCODE pipeline.

  * probe_media / transcode_media_to_mp4 / transcode_media_to_audio tool wrappers
    (ffmpeg/ffprobe fully mocked — no real binaries).
  * process_media_transcode handler: Mode 1 passthrough vs transcode, Mode 2
    extraction scan.
  * handle_media_transcode search-index handler (insert + scoped delete).

Run:
    python -m unittest ci.test_media_transcode -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.analyses import _common as common_mod
from worker.arcworker.analyses import media as media_mod
from worker.arcworker.analysis import AnalysisWorker
from worker.arcworker.tools import media_transcode as mt

# A deterministic stand-in for the streaming file hash so the content-keyed
# transcode cache can run without real files on disk.
_FAKE_HASH = ('0' * 32, 'f' * 64, 4)


class _FakeProc:
    def __init__(self, returncode=0, stdout=b'', stderr=b''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ─────────────────────────────────────────────────────────────────────────────
# Tool wrappers
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeMedia(unittest.TestCase):

    def _probe_with(self, streams, fmt):
        payload = json.dumps({'streams': streams, 'format': fmt}).encode()
        with patch.object(mt, 'run_tool_with_output',
                          return_value=(_FakeProc(0, payload), {})):
            return mt.probe_media(Path('/x'))

    def test_video_with_audio(self):
        r = self._probe_with(
            [{'codec_type': 'video', 'codec_name': 'h264', 'width': 640, 'height': 480,
              'avg_frame_rate': '30000/1001'},
             {'codec_type': 'audio', 'codec_name': 'aac', 'sample_rate': '44100', 'channels': 2}],
            {'format_name': 'mov,mp4,m4a', 'duration': '12.5'})
        self.assertTrue(r['success'])
        self.assertTrue(r['has_video'])
        self.assertTrue(r['has_audio'])
        self.assertEqual(r['video_codec'], 'h264')
        self.assertEqual(r['width'], 640)
        self.assertAlmostEqual(r['frame_rate'], 30000 / 1001, places=2)
        self.assertEqual(r['audio_codec'], 'aac')
        self.assertEqual(r['sample_rate'], 44100)
        self.assertEqual(r['channels'], 2)
        self.assertAlmostEqual(r['duration_seconds'], 12.5)

    def test_audio_only(self):
        r = self._probe_with(
            [{'codec_type': 'audio', 'codec_name': 'mp3', 'sample_rate': '44100', 'channels': 2}],
            {'format_name': 'mp3', 'duration': '180'})
        self.assertTrue(r['success'])
        self.assertFalse(r['has_video'])
        self.assertTrue(r['has_audio'])
        self.assertIsNone(r['video_codec'])

    def test_ffprobe_failure(self):
        with patch.object(mt, 'run_tool_with_output',
                          return_value=(_FakeProc(1, b'', b'boom'), {})):
            r = mt.probe_media(Path('/x'))
        self.assertFalse(r['success'])


class TestTranscodeTools(unittest.TestCase):

    def test_mp4_success(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / 'o.mp4'

            def _fake(cmd, timeout=None):
                # The first call is the transcode; create the output file.
                if str(out) in cmd:
                    out.write_bytes(b'data')
                return _FakeProc(0), {}

            with patch.object(mt, 'run_tool_with_output', side_effect=_fake):
                r = mt.transcode_media_to_mp4(d / 'in.avi', out, work_dir=d, has_audio=True)
            self.assertTrue(r['success'])
            self.assertEqual(r['output_type'], 'mp4')

    def test_mp4_failure_sets_stage(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with patch.object(mt, 'run_tool_with_output',
                              return_value=(_FakeProc(1, stderr=b'bad'), {})):
                r = mt.transcode_media_to_mp4(d / 'in.avi', d / 'o.mp4', work_dir=d)
            self.assertFalse(r['success'])
            self.assertEqual(r['stage'], 'transcode')

    def test_audio_success(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / 'o.m4a'

            def _fake(cmd, timeout=None):
                out.write_bytes(b'data')
                return _FakeProc(0), {}

            with patch.object(mt, 'run_tool_with_output', side_effect=_fake):
                r = mt.transcode_media_to_audio(d / 'in.wma', out, work_dir=d)
            self.assertTrue(r['success'])
            self.assertEqual(r['output_type'], 'm4a')
            self.assertTrue(r['audio_only'])


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────

def _worker():
    w = MagicMock(spec=AnalysisWorker)
    w.save_output_file.side_effect = lambda p, name, subdir=None: f'out/{name}'
    w.api = MagicMock()
    w.api.get_transcode_cache.return_value = None  # default: content-cache miss
    return w


def _probe(has_video, video_codec=None, audio_codec=None, has_audio=False):
    return {
        'success': True, 'has_video': has_video, 'has_audio': has_audio,
        'container_format': 'fmt', 'video_codec': video_codec, 'width': 320,
        'height': 240, 'frame_rate': 25.0, 'audio_codec': audio_codec,
        'sample_rate': 44100, 'channels': 2, 'duration_seconds': 10.0,
    }


def _details_from_complete(worker):
    """Extract the details dict from the worker.complete_analysis call."""
    kwargs = worker.complete_analysis.call_args.kwargs
    return json.loads(kwargs['details'])


class TestMode1Direct(unittest.TestCase):

    def _run(self, filename, probe_result, transcode_result=None):
        w = _worker()
        w.get_input_path.return_value = Path('/work/in')
        analysis = {'id': 1, 'uuid': 'u', 'hints': None}
        artefact = {'artefact_type': 'video' if filename.endswith(
            ('.mp4', '.avi', '.mov')) else 'audio',
            'original_filename': filename, 'uuid': 'a'}
        with patch.object(media_mod, 'probe_media', return_value=probe_result), \
             patch.object(common_mod, 'compute_file_hash', return_value=_FAKE_HASH), \
             patch.object(media_mod, 'transcode_media_to_mp4',
                          return_value=transcode_result or {'success': True, 'poster_path': None}), \
             patch.object(media_mod, 'transcode_media_to_audio',
                          return_value=transcode_result or {'success': True}), \
             patch.object(media_mod, 'extract_media_poster', return_value=None):
            media_mod.process_media_transcode(w, analysis, artefact, Path('/work'))
        return w

    def test_native_mp4_passthrough(self):
        w = self._run('clip.mp4', _probe(True, 'h264', 'aac', has_audio=True))
        w.complete_analysis.assert_called_once()
        w.fail_analysis.assert_not_called()
        d = _details_from_complete(w)
        entry = d['transcoded'][0]
        self.assertTrue(entry['passthrough'])
        self.assertIsNone(entry['mp4_output_path'])

    def test_avi_transcoded(self):
        w = self._run('clip.avi', _probe(True, 'mpeg4', 'mp3', has_audio=True),
                      transcode_result={'success': True, 'poster_path': None})
        d = _details_from_complete(w)
        entry = d['transcoded'][0]
        self.assertFalse(entry['passthrough'])
        # Output is content-addressed: stored as movie.<ext> under a hash subdir.
        self.assertEqual(entry['mp4_output_path'], 'out/movie.mp4')
        self.assertEqual(entry['input_sha256'], _FAKE_HASH[1])

    def test_probe_failure_fails_analysis(self):
        w = self._run('clip.avi', {'success': False, 'error': 'bad'})
        w.fail_analysis.assert_called_once()


class TestMode2Extraction(unittest.TestCase):

    def test_scan_mix_passthrough_and_transcode(self):
        from worker.arcworker.analyses._common import BatchScanResult
        w = _worker()
        analysis = {'id': 1, 'uuid': 'u', 'hints': json.dumps({'partition_uuid': 'p'})}
        artefact = {'artefact_type': 'raw_sector', 'uuid': 'a'}

        files = [
            {'path': 'm/clip.avi', 'filename': 'clip.avi'},
            {'path': 'm/song.mp3', 'filename': 'song.mp3'},
        ]
        scan = BatchScanResult(extraction_path='ext', files=files,
                               path_prefix='', partition_uuid='p')

        def _iter(self, fs, ep, wd, path_prefix='', reporter=None, on_missing=None):
            for fd in fs:
                yield fd, Path('/disk/' + fd['filename']), fd['path']

        def _probe_by_path(path):
            if str(path).endswith('.avi'):
                return _probe(True, 'mpeg4', 'mp3', has_audio=True)
            return _probe(False, None, 'mp3', has_audio=True)

        with patch.object(media_mod, 'scan_partition_files', return_value=scan), \
             patch.object(media_mod, 'iter_resolved_files', _iter), \
             patch.object(media_mod, 'probe_media', side_effect=_probe_by_path), \
             patch.object(common_mod, 'compute_file_hash', return_value=_FAKE_HASH), \
             patch.object(media_mod, 'transcode_media_to_mp4',
                          return_value={'success': True, 'poster_path': None}), \
             patch.object(media_mod, 'transcode_media_to_audio',
                          return_value={'success': True}), \
             patch.object(media_mod, 'extract_media_poster', return_value=None):
            media_mod.process_media_transcode(w, analysis, artefact, Path('/work'))

        d = _details_from_complete(w)
        self.assertEqual(len(d['transcoded']), 2)
        by_path = {e['file_path']: e for e in d['transcoded']}
        # AVI was transcoded; MP3 (passthrough) was not.
        self.assertTrue(by_path['m/clip.avi']['mp4_output_path'])
        self.assertIsNone(by_path['m/song.mp3']['mp4_output_path'])
        self.assertEqual(by_path['m/song.mp3']['media_kind'], 'audio')

    def test_no_media_files_completes_empty(self):
        from worker.arcworker.analyses._common import BatchScanResult
        w = _worker()
        analysis = {'id': 1, 'uuid': 'u', 'hints': json.dumps({'partition_uuid': 'p'})}
        artefact = {'artefact_type': 'raw_sector', 'uuid': 'a'}
        scan = BatchScanResult(extraction_path=None, files=[], path_prefix='', partition_uuid='p')
        with patch.object(media_mod, 'scan_partition_files', return_value=scan):
            media_mod.process_media_transcode(w, analysis, artefact, Path('/work'))
        d = _details_from_complete(w)
        self.assertEqual(d['files_scanned'], 0)
        self.assertEqual(d['transcoded'], [])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
