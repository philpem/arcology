"""Tests for shared path-navigation helpers used by the File Viewer and Viewer."""

import os
import sys
import unittest

# Ensure the repo root is on sys.path so myapp is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('WORKER_API_KEY', 'test')

from myapp.utils.path_nav import compute_subdirectories, split_path_segments


class TestComputeSubdirectories(unittest.TestCase):
    """compute_subdirectories should return natural-sorted immediate children."""

    def test_root_listing(self):
        paths = ['a/x', 'a/y/z', 'b/q', 'c']
        self.assertEqual(compute_subdirectories(paths, ''), ['a', 'b'])

    def test_nested_path(self):
        paths = ['a/x', 'a/y/z', 'b/q', 'c']
        self.assertEqual(compute_subdirectories(paths, 'a/'), ['y'])

    def test_ignores_unrelated_paths(self):
        paths = ['boot/x', 'boot/y', 'other/z']
        self.assertEqual(compute_subdirectories(paths, 'boot/'), [])

    def test_deep_siblings(self):
        paths = ['boot/bin/a', 'boot/bin/b', 'boot/lib/c', 'boot/etc/d/e']
        self.assertEqual(
            compute_subdirectories(paths, 'boot/'),
            ['bin', 'etc', 'lib'],
        )

    def test_deduplication(self):
        # Same first component from many files collapses to one entry.
        paths = ['a/1', 'a/2', 'a/3', 'a/4']
        self.assertEqual(compute_subdirectories(paths, ''), ['a'])

    def test_single_level_file_no_phantom(self):
        # A file at the current level (no further '/' in relative) must not
        # produce a phantom subdirectory entry.
        paths = ['readme']
        self.assertEqual(compute_subdirectories(paths, ''), [])

    def test_trailing_file_at_current_level(self):
        paths = ['boot/readme', 'boot/bin/ls']
        self.assertEqual(compute_subdirectories(paths, 'boot/'), ['bin'])

    def test_natural_case_insensitive_sort(self):
        paths = ['item2/x', 'item10/x', 'Item1/x']
        # Natural sort: Item1, item2, item10 (case-insensitive, numeric-aware)
        self.assertEqual(
            compute_subdirectories(paths, ''),
            ['Item1', 'item2', 'item10'],
        )

    def test_empty_input(self):
        self.assertEqual(compute_subdirectories([], ''), [])
        self.assertEqual(compute_subdirectories([], 'boot/'), [])

    def test_ignores_empty_strings(self):
        paths = ['', 'a/b']
        self.assertEqual(compute_subdirectories(paths, ''), ['a'])


class TestSplitPathSegments(unittest.TestCase):
    """split_path_segments should produce breadcrumb (label, cumulative) pairs."""

    def test_empty(self):
        self.assertEqual(split_path_segments(''), [])
        self.assertEqual(split_path_segments(None or ''), [])

    def test_single(self):
        self.assertEqual(split_path_segments('boot/'), [('boot', 'boot/')])

    def test_multi(self):
        self.assertEqual(
            split_path_segments('boot/bin/'),
            [('boot', 'boot/'), ('bin', 'boot/bin/')],
        )

    def test_missing_trailing_slash_tolerated(self):
        self.assertEqual(
            split_path_segments('boot/bin'),
            [('boot', 'boot/'), ('bin', 'boot/bin/')],
        )

    def test_slash_only(self):
        self.assertEqual(split_path_segments('/'), [])


if __name__ == '__main__':
    unittest.main()


# vim: ts=4 sw=4 et
