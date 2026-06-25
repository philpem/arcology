"""
Unit tests for the Acorn Replay / ARMovie codec-type name tables.

Covers the video/sound format-number → name lookups in
arcology_shared/replay_codecs.py: known numbers resolve to a name, unknown
numbers and None return None, and the tables stay internally consistent.

Run:
    python -m unittest ci.test_replay_codecs -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from arcology_shared.replay_codecs import (  # noqa: E402
    SOUND_CODEC_NAMES,
    VIDEO_CODEC_NAMES,
    sound_codec_name,
    video_codec_name,
)


class TestVideoCodecNames(unittest.TestCase):
    def test_known_native_codecs(self):
        self.assertEqual(video_codec_name(1), 'Moving Lines')
        self.assertEqual(video_codec_name(7), 'Moving Blocks')
        self.assertEqual(video_codec_name(17), 'Moving Blocks HQ')
        self.assertEqual(video_codec_name(19), 'Super Moving Blocks')
        self.assertEqual(video_codec_name(20), 'Moving Blocks Beta')

    def test_sound_only_marker(self):
        # Video format 0 means the movie carries sound only.
        self.assertEqual(video_codec_name(0), 'Sound only (no video)')

    def test_known_moviefs_codec(self):
        self.assertEqual(video_codec_name(602), 'Cinepak')

    def test_unknown_number_is_none(self):
        self.assertIsNone(video_codec_name(99999))

    def test_none_is_none(self):
        self.assertIsNone(video_codec_name(None))


class TestSoundCodecNames(unittest.TestCase):
    def test_silent(self):
        self.assertEqual(sound_codec_name(0), 'Silent (no sound)')

    def test_known_codec(self):
        self.assertEqual(sound_codec_name(1), 'VIDC (8-bit logarithmic)')

    def test_unknown_number_is_none(self):
        self.assertIsNone(sound_codec_name(12345))

    def test_none_is_none(self):
        self.assertIsNone(sound_codec_name(None))


class TestTableIntegrity(unittest.TestCase):
    def test_all_keys_are_ints(self):
        for key in VIDEO_CODEC_NAMES:
            self.assertIsInstance(key, int)
        for key in SOUND_CODEC_NAMES:
            self.assertIsInstance(key, int)

    def test_all_names_are_nonempty_strings(self):
        for name in (*VIDEO_CODEC_NAMES.values(), *SOUND_CODEC_NAMES.values()):
            self.assertIsInstance(name, str)
            self.assertTrue(name.strip())


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
