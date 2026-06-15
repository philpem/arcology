"""
Unit tests for the Acorn Replay / ARMovie header parser.

Covers the 21-line text header parsing, the "number of chunks is the highest
index" off-by-one, derived statistics (duration, sound-only, key-frame/poster
presence), leading-token parsing of numeric lines that carry descriptive prose,
trailing-CR tolerance, the catalogue sound-track count, and error handling.

The parser (worker/arcworker/tools/armovie.py) is pure stdlib, so it runs in CI.

Run:
    python -m unittest ci.test_armovie_parser -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools.armovie import (  # noqa: E402
    ArmovieParseError,
    parse_armovie_header,
)

# A realistic full-video header modelled on LionFish19 (type 19, 160x128,
# 12.5 fps, 25 frames/chunk, highest chunk index 14 → 15 chunks, one sound
# region per chunk).  Numeric lines carry descriptive prose to exercise the
# leading-token parsing.
_FULL_LINES = [
    "ARMovie",
    "Lion fish in the Red Sea",
    "(C) BBC",
    "Natural History Unit",
    "19 video format",
    "160 pixels",
    "128 pixels",
    "16 bits per pixel",
    "12.5 frames per second",
    "1 sound format",
    "44100 Hz",
    "1 channel",
    "8 bits per sample",
    "25 frames per chunk",
    "14 number of chunks",
    "184000 even chunk size",
    "184000 odd chunk size",
    "100000 catalogue offset",
    "200000 sprite offset",
    "2048 size of sprite",
    "-1 (no keys)",
]


def _build(lines, *, trailing_cr=False, final_newline=True):
    sep = "\r\n" if trailing_cr else "\n"
    text = sep.join(lines)
    if final_newline:
        text += sep
    return text.encode("latin-1")


class TestArmovieParser(unittest.TestCase):
    def test_full_header_fields(self):
        meta = parse_armovie_header(_build(_FULL_LINES))
        self.assertEqual(meta["title"], "Lion fish in the Red Sea")
        self.assertEqual(meta["copyright"], "(C) BBC")
        self.assertEqual(meta["author"], "Natural History Unit")
        self.assertEqual(meta["video_format"], 19)
        self.assertEqual(meta["width"], 160)
        self.assertEqual(meta["height"], 128)
        self.assertEqual(meta["pixel_depth"], 16)
        self.assertEqual(meta["frame_rate"], 12.5)
        self.assertEqual(meta["sound_format"], 1)
        self.assertEqual(meta["sound_rate"], 44100)
        self.assertEqual(meta["sound_channels"], 1)
        self.assertEqual(meta["sound_precision"], 8)
        self.assertEqual(meta["frames_per_chunk"], 25.0)

    def test_number_of_chunks_off_by_one(self):
        # Line 15 holds the HIGHEST index (14); the entry count is 15.
        meta = parse_armovie_header(_build(_FULL_LINES))
        self.assertEqual(meta["chunks_highest_index"], 14)
        self.assertEqual(meta["number_of_chunks"], 15)

    def test_duration_seconds(self):
        # 25 frames/chunk * 15 chunks / 12.5 fps = 30.0 s
        meta = parse_armovie_header(_build(_FULL_LINES))
        self.assertAlmostEqual(meta["duration_seconds"], 30.0)

    def test_flags_full_video(self):
        meta = parse_armovie_header(_build(_FULL_LINES))
        self.assertFalse(meta["sound_only"])
        self.assertFalse(meta["has_key_frames"])      # keys_offset == -1
        self.assertTrue(meta["has_poster_sprite"])    # sprite_size > 0

    def test_trailing_cr_tolerated(self):
        meta = parse_armovie_header(_build(_FULL_LINES, trailing_cr=True))
        self.assertEqual(meta["video_format"], 19)
        self.assertEqual(meta["number_of_chunks"], 15)

    def test_sound_only_movie(self):
        lines = list(_FULL_LINES)
        lines[4] = "0 video format"   # sound-only
        lines[5] = "0 pixels"
        lines[6] = "0 pixels"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 0)
        self.assertTrue(meta["sound_only"])

    def test_with_key_table(self):
        lines = list(_FULL_LINES)
        lines[20] = "250000 offset to keys"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["keys_offset"], 250000)
        self.assertTrue(meta["has_key_frames"])

    def test_fractional_frames_per_chunk(self):
        lines = list(_FULL_LINES)
        lines[13] = "12.5 frames per chunk"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["frames_per_chunk"], 12.5)

    def test_empty_title_omitted(self):
        lines = list(_FULL_LINES)
        lines[1] = ""   # empty title
        meta = parse_armovie_header(_build(lines))
        self.assertNotIn("title", meta)

    def test_bad_magic_raises(self):
        lines = list(_FULL_LINES)
        lines[0] = "NotAMovie"
        with self.assertRaises(ArmovieParseError):
            parse_armovie_header(_build(lines))

    def test_truncated_header_raises(self):
        with self.assertRaises(ArmovieParseError):
            parse_armovie_header(_build(_FULL_LINES[:10]))

    def test_video_format_generic_descriptor_no_label(self):
        # "19 video format" → number 19, generic descriptor dropped (no label).
        meta = parse_armovie_header(_build(_FULL_LINES))
        self.assertEqual(meta["video_format"], 19)
        self.assertNotIn("video_label", meta)

    def test_video_format_attached_suffix(self):
        # CFC / Anglia TV style: "1K" → codec number 1 with label "1K"
        # (the whole token is kept because the suffix is attached, no space).
        lines = list(_FULL_LINES)
        lines[4] = "1K"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 1)
        self.assertEqual(meta["video_label"], "1K")

    def test_video_format_named_codec(self):
        # "1 Moving Lines" → number 1, label "Moving Lines" (space-separated
        # remainder is a genuine codec name, kept verbatim).
        lines = list(_FULL_LINES)
        lines[4] = "1 Moving Lines"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 1)
        self.assertEqual(meta["video_label"], "Moving Lines")

    def test_video_format_named_codec_blocks(self):
        lines = list(_FULL_LINES)
        lines[4] = "2 Moving Blocks"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 2)
        self.assertEqual(meta["video_label"], "Moving Blocks")

    def test_video_format_bare_number_no_label(self):
        lines = list(_FULL_LINES)
        lines[4] = "19"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 19)
        self.assertNotIn("video_label", meta)

    def test_sound_only_no_label(self):
        # Format 0 (sound-only) carries no codec label.
        lines = list(_FULL_LINES)
        lines[4] = "0"
        meta = parse_armovie_header(_build(lines))
        self.assertEqual(meta["video_format"], 0)
        self.assertNotIn("video_label", meta)

    def test_catalogue_sound_track_count(self):
        # Place a small catalogue right after the header and point line 18 at it.
        header_lines = list(_FULL_LINES)
        header_lines[14] = "2 number of chunks"   # 3 chunks

        # The catalogue offset must equal the header's byte length, but writing
        # the offset into the header changes that length — iterate to a stable
        # fixed point (digit count converges quickly).
        offset = 0
        for _ in range(8):
            header_lines[17] = f"{offset} catalogue offset"
            header_bytes = _build(header_lines)
            if len(header_bytes) == offset:
                break
            offset = len(header_bytes)

        # Two sound tracks on one chunk, three on another.
        catalogue = (
            "0,1000;500;500\n"
            "2000,1000;500\n"
            "4000,1000;500;500;500\n"
        ).encode("latin-1")
        data = header_bytes + catalogue
        meta = parse_armovie_header(data)
        self.assertEqual(meta["sound_track_count"], 3)


if __name__ == "__main__":
    unittest.main()

# vim: ts=4 sw=4 et
