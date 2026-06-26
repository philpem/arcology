"""Tests for the shared content classifier (arcology_shared.content_categories).

classify_content() is the single source of truth that replaced five separate
per-file selection predicates (archive / convertible / module / replay / media).
These tests pin each category's detection and confirm the result stays a set of
exactly the expected categories so the follow-up dispatcher gates correctly.

Run:
    python -m unittest ci.test_content_categories -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from arcology_shared.content_categories import (
    ContentCategory,
    classify_content,
    present_content_categories,
)

C = ContentCategory


class TestClassifyContent(unittest.TestCase):
    def test_archive_by_extension(self):
        self.assertEqual(classify_content('disk.zip', None), {C.ARCHIVE})

    def test_archive_by_riscos_filetype(self):
        # &3FB = ArcFS; case-insensitive on the filetype hex.
        self.assertIn(C.ARCHIVE, classify_content('Archive', '3fb'))
        self.assertIn(C.ARCHIVE, classify_content('Archive', '3FB'))

    def test_convertible_sprite_by_filetype(self):
        self.assertEqual(classify_content('Logo', 'ff9'), {C.CONVERTIBLE})

    def test_convertible_image_by_extension(self):
        self.assertEqual(classify_content('photo.PNG', None), {C.CONVERTIBLE})

    def test_riscos_module_by_filetype(self):
        self.assertEqual(classify_content('SharedCLib', 'ffa'), {C.RISCOS_MODULE})

    def test_replay_by_filetype_and_extension(self):
        self.assertEqual(classify_content('Demo', 'ae7'), {C.REPLAY})
        self.assertEqual(classify_content('demo.rpl', None), {C.REPLAY})

    def test_media_by_extension_and_filetype(self):
        self.assertEqual(classify_content('clip.avi', None), {C.MEDIA})
        # &071 = AVI (RISC OS media filetype).
        self.assertEqual(classify_content('clip', '071'), {C.MEDIA})

    def test_plain_file_matches_nothing(self):
        self.assertEqual(classify_content('ReadMe', None), set())
        self.assertEqual(classify_content('source.bas', 'ffb'), set())

    def test_empty_and_none_inputs_are_safe(self):
        self.assertEqual(classify_content('', None), set())
        self.assertEqual(classify_content('', ''), set())

    def test_result_is_a_set(self):
        self.assertIsInstance(classify_content('disk.zip', None), set)


class TestPresentContentCategories(unittest.TestCase):
    def test_union_across_files_skipping_directories(self):
        files = [
            {'path': 'Games', 'is_directory': True},          # skipped
            {'path': 'Games/demo', 'risc_os_filetype': 'ae7'},  # REPLAY
            {'path': 'Games/clip.avi'},                         # MEDIA
            {'path': 'Games/readme'},                           # nothing
        ]
        self.assertEqual(
            present_content_categories(files), {C.REPLAY, C.MEDIA})

    def test_empty_when_no_classified_files(self):
        files = [
            {'path': 'dir', 'is_directory': True},
            {'path': 'dir/source.bas', 'risc_os_filetype': 'ffb'},
        ]
        self.assertEqual(present_content_categories(files), set())

    def test_filename_key_is_honoured(self):
        # enumerate dicts use 'path'; DB-shaped dicts may use 'filename'.
        self.assertEqual(
            present_content_categories([{'filename': 'x.zip'}]), {C.ARCHIVE})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
