"""
Unit tests for EXTENSION_MAP and ANALYSIS_MAP in myapp/services/artefact_types.py.

Checks that every enum value referenced in the maps actually exists in
arcology_shared/enums.py, and that every ArtefactType has an ANALYSIS_MAP entry.
Catches the common mistake of adding a new type to the enum but forgetting
to update one of the maps.

Requires pip install (Flask, SQLAlchemy, etc. must be available).

Run:
    python -m unittest ci.test_artefact_map -v
"""

import os
import sys
import unittest

# Ensure the repo root is on sys.path so ``myapp`` and ``arcology_shared`` are importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# arcology_shared.enums has no external dependencies — import directly for the
# canonical set of valid enum members.
# myapp.services.artefact_types imports Flask/SQLAlchemy at module level.
# Those packages are available in the app-tests CI job (pip install is done
# before these tests run).
from arcology_shared.enums import AnalysisType, ArtefactType
from myapp.services.artefact_types import ANALYSIS_MAP, EXTENSION_MAP, detect_artefact_type

_ALL_ARTEFACT_TYPES = set(ArtefactType)
_ALL_ANALYSIS_TYPES = set(AnalysisType)


class TestExtensionMap(unittest.TestCase):
    """EXTENSION_MAP maps file extensions → ArtefactType."""

    def test_values_are_valid_artefact_types(self):
        """Every value in EXTENSION_MAP must be a member of ArtefactType."""
        invalid = {
            ext: val
            for ext, val in EXTENSION_MAP.items()
            if val not in _ALL_ARTEFACT_TYPES
        }
        self.assertFalse(
            invalid,
            f'EXTENSION_MAP values not in ArtefactType: {invalid}',
        )

    def test_keys_start_with_dot_or_comma(self):
        """Extension keys should start with '.' or ',' (RISC OS comma-suffix)."""
        bad = [k for k in EXTENSION_MAP if not k.startswith(('.', ','))]
        self.assertFalse(bad, f'EXTENSION_MAP keys without leading dot or comma: {bad}')

    def test_keys_are_lowercase(self):
        """Extension keys should be lowercase for case-insensitive matching."""
        bad = [k for k in EXTENSION_MAP if k != k.lower()]
        self.assertFalse(bad, f'EXTENSION_MAP keys not lowercase: {bad}')


class TestDetectArtefactType(unittest.TestCase):
    """detect_artefact_type() handles plain and compressed extensions."""

    def _check(self, filename, expected):
        result = detect_artefact_type(filename)
        self.assertEqual(result, expected,
                         f'{filename!r} → {result} (expected {expected})')

    def test_plain_scp(self):
        self._check('disc.scp', ArtefactType.SCP)

    def test_plain_dfi(self):
        self._check('disc.dfi', ArtefactType.DFI)

    def test_dfi_gz(self):
        self._check('disc.dfi.gz', ArtefactType.DFI)

    def test_dfi_bz2(self):
        self._check('diamondmm_stealth64-video-2001_win95_v1.02_100MHz.dfi.bz2', ArtefactType.DFI)

    def test_dfi_zst(self):
        self._check('disc.dfi.zst', ArtefactType.DFI)

    def test_scp_gz(self):
        self._check('disc.scp.gz', ArtefactType.SCP)

    def test_scp_bz2(self):
        self._check('disc.scp.bz2', ArtefactType.SCP)

    def test_unknown_extension(self):
        self._check('file.xyz', ArtefactType.UNKNOWN)

    def test_compressed_unknown(self):
        self._check('file.xyz.gz', ArtefactType.UNKNOWN)

    def test_dd_bz2_explicit(self):
        self._check('drive.dd.bz2', ArtefactType.DD_BZ2)

    def test_tar_gz_explicit(self):
        self._check('archive.tar.gz', ArtefactType.TARGZ)

    def test_case_insensitive(self):
        self._check('DISC.DFI.GZ', ArtefactType.DFI)


# Artefact types that intentionally have NO automatic analyses.  CHECKSUM_COMPUTE
# is still queued for them on direct upload (it is added independent of
# ANALYSIS_MAP), so an empty list is a deliberate "nothing else to do" — e.g.
# SIDECAR companion files (ddrescue .map / readme / checksums attached to a disk
# image).  Listing them here keeps the "every type is wired up on purpose" guard
# while allowing a genuinely analysis-free type.
_INTENTIONALLY_NO_ANALYSES = {ArtefactType.SIDECAR}


class TestAnalysisMap(unittest.TestCase):
    """ANALYSIS_MAP maps ArtefactType → list[AnalysisType]."""

    def test_keys_are_valid_artefact_types(self):
        """Every key in ANALYSIS_MAP must be a member of ArtefactType."""
        invalid = [k for k in ANALYSIS_MAP if k not in _ALL_ARTEFACT_TYPES]
        self.assertFalse(
            invalid,
            f'ANALYSIS_MAP keys not in ArtefactType: {invalid}',
        )

    def test_values_are_valid_analysis_types(self):
        """Every item in every value list must be a member of AnalysisType."""
        invalid = {}
        for artefact_type, analysis_list in ANALYSIS_MAP.items():
            bad = [a for a in analysis_list if a not in _ALL_ANALYSIS_TYPES]
            if bad:
                invalid[artefact_type] = bad
        self.assertFalse(
            invalid,
            f'ANALYSIS_MAP values not in AnalysisType: {invalid}',
        )

    def test_values_are_non_empty_lists(self):
        """Every entry maps to ≥1 analysis, unless intentionally analysis-free.

        An accidental empty list would silently drop a type's analyses, so the
        only permitted empties are those explicitly declared in
        _INTENTIONALLY_NO_ANALYSES.
        """
        empty = [k for k, v in ANALYSIS_MAP.items()
                 if not v and k not in _INTENTIONALLY_NO_ANALYSES]
        self.assertFalse(empty, f'ANALYSIS_MAP entries with empty lists: {empty}')

    def test_all_artefact_types_covered(self):
        """Every ArtefactType member should have an entry in ANALYSIS_MAP.

        A missing entry means a newly-added type will silently get no
        automatic analysis queued on upload.
        """
        missing = _ALL_ARTEFACT_TYPES - set(ANALYSIS_MAP.keys())
        self.assertFalse(
            missing,
            f'ArtefactType members not in ANALYSIS_MAP: {missing}\n'
            'Add an entry to ANALYSIS_MAP in myapp/blueprints/artefacts.py.',
        )

    def test_image_extensions_present(self):
        """Common image extensions must all map to ArtefactType.IMAGE."""
        from arcology_shared.enums import ArtefactType
        for ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff',
                    '.wmf', '.emf'):
            self.assertEqual(
                EXTENSION_MAP.get(ext), ArtefactType.IMAGE,
                f'Expected EXTENSION_MAP[{ext!r}] == ArtefactType.IMAGE',
            )

    def test_media_extensions_present(self):
        """Video/audio container extensions map to VIDEO/AUDIO."""
        for ext in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.mpg', '.mpeg', '.m2v'):
            self.assertEqual(EXTENSION_MAP.get(ext), ArtefactType.VIDEO,
                             f'Expected EXTENSION_MAP[{ext!r}] == VIDEO')
        for ext in ('.mp3', '.ogg', '.wav', '.flac', '.m4a', '.wma'):
            self.assertEqual(EXTENSION_MAP.get(ext), ArtefactType.AUDIO,
                             f'Expected EXTENSION_MAP[{ext!r}] == AUDIO')

    def test_media_queue_includes_transcode(self):
        """VIDEO/AUDIO uploads queue MEDIA_TRANSCODE."""
        for atype in (ArtefactType.VIDEO, ArtefactType.AUDIO):
            self.assertIn(AnalysisType.MEDIA_TRANSCODE, ANALYSIS_MAP[atype])


class TestMediaPlayability(unittest.TestCase):
    """Codec-aware passthrough vs transcode decision (media_is_browser_playable)."""

    def setUp(self):
        from arcology_shared.artefact_types import (
            MEDIA_EXTENSIONS,
            media_is_browser_playable,
        )
        self.playable = media_is_browser_playable
        self.media_exts = MEDIA_EXTENSIONS

    def test_extension_sets_cover_detect(self):
        """Every MEDIA_EXTENSIONS entry detects as VIDEO or AUDIO."""
        for ext in self.media_exts:
            self.assertIn(detect_artefact_type('x' + ext),
                          (ArtefactType.VIDEO, ArtefactType.AUDIO), ext)

    def test_h264_mp4_passthrough(self):
        self.assertTrue(self.playable('a.mp4', has_video=True,
                                      video_codec='h264', audio_codec='aac'))

    def test_h264_mov_passthrough(self):
        # QuickTime container with H.264 plays natively — must not be transcoded.
        self.assertTrue(self.playable('a.mov', has_video=True,
                                      video_codec='h264', audio_codec='aac'))

    def test_avi_divx_transcode(self):
        self.assertFalse(self.playable('a.avi', has_video=True,
                                       video_codec='mpeg4', audio_codec='mp3'))

    def test_mpeg2_transcode(self):
        self.assertFalse(self.playable('a.mpg', has_video=True,
                                       video_codec='mpeg2video', audio_codec='mp2'))

    def test_hevc_mp4_transcode(self):
        # HEVC is not universally supported — transcode even in an MP4 container.
        self.assertFalse(self.playable('a.mp4', has_video=True,
                                       video_codec='hevc', audio_codec='aac'))

    def test_webm_vp9_passthrough(self):
        self.assertTrue(self.playable('a.webm', has_video=True,
                                      video_codec='vp9', audio_codec='opus'))

    def test_mp3_passthrough(self):
        self.assertTrue(self.playable('a.mp3', has_video=False,
                                      video_codec=None, audio_codec='mp3'))

    def test_wma_transcode(self):
        self.assertFalse(self.playable('a.wma', has_video=False,
                                       video_codec=None, audio_codec='wmav2'))

    def test_wav_pcm_passthrough(self):
        self.assertTrue(self.playable('a.wav', has_video=False,
                                      video_codec=None, audio_codec='pcm_s16le'))

    def test_audio_only_in_video_container_passthrough(self):
        # An audio-only stream in a browser-native video container (AAC in .mp4,
        # Opus in .webm) plays in an HTML5 element — must NOT be transcoded.
        self.assertTrue(self.playable('a.mp4', has_video=False,
                                      video_codec=None, audio_codec='aac'))
        self.assertTrue(self.playable('a.webm', has_video=False,
                                      video_codec=None, audio_codec='opus'))
        self.assertTrue(self.playable('a.mov', has_video=False,
                                      video_codec=None, audio_codec='aac'))

    def test_audio_only_in_video_container_bad_codec_transcodes(self):
        # ...but only when the audio codec itself is browser-decodable.
        self.assertFalse(self.playable('a.mp4', has_video=False,
                                       video_codec=None, audio_codec='ac3'))

    def test_streamless_file_not_passthrough(self):
        # A corrupt/empty file with a media extension but no decodable audio
        # stream (ffprobe reports no codec) must NOT be passed through as a
        # broken player — it should fall through to transcode.
        self.assertFalse(self.playable('a.mp3', has_video=False,
                                       video_codec=None, audio_codec=None))
        self.assertFalse(self.playable('a.mp4', has_video=False,
                                       video_codec=None, audio_codec=None))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
