"""Tests for shared path-navigation helpers used by the File Viewer and Viewer."""

import os
import sys
import unittest

# Ensure the repo root is on sys.path so myapp is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('WORKER_API_KEY', 'test')

from myapp.utils.path_nav import build_directory_tree, compute_subdirectories, split_path_segments


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


class _FakePartition:
    """Minimal stand-in for the Partition ORM object used by build_directory_tree."""
    def __init__(self, uuid, index=0):
        self.uuid = uuid
        self.partition_index = index


class TestBuildDirectoryTree(unittest.TestCase):
    """build_directory_tree constructs a partition-rooted nested directory tree."""

    def _tree(self, path_rows, partitions, archive_paths=None):
        return build_directory_tree(path_rows, partitions, archive_paths=archive_paths)

    # ── Basic single-partition cases ──────────────────────────────────────────

    def test_empty_partition_omitted(self):
        p = _FakePartition('p1')
        result = self._tree([], [p])
        self.assertEqual(result, [])

    def test_flat_root_files_no_dirs(self):
        p = _FakePartition('p1')
        rows = [('readme.txt', 'p1'), ('setup.py', 'p1')]
        result = self._tree(rows, [p])
        # No subdirectories — root-level files only
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['children'], [])

    def test_single_level_dirs(self):
        p = _FakePartition('p1')
        rows = [('a/file1', 'p1'), ('b/file2', 'p1'), ('b/file3', 'p1')]
        result = self._tree(rows, [p])
        children = result[0]['children']
        names = [c['name'] for c in children]
        self.assertEqual(names, ['a', 'b'])

    def test_nested_dirs(self):
        p = _FakePartition('p1')
        rows = [('a/b/c/file', 'p1')]
        result = self._tree(rows, [p])
        a = result[0]['children']
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]['name'], 'a')
        self.assertEqual(a[0]['path'], 'a/')
        b = a[0]['children']
        self.assertEqual(b[0]['name'], 'b')
        c = b[0]['children']
        self.assertEqual(c[0]['name'], 'c')
        self.assertEqual(c[0]['children'], [])

    def test_natural_sort_order(self):
        p = _FakePartition('p1')
        rows = [('item10/f', 'p1'), ('item2/f', 'p1'), ('Item1/f', 'p1')]
        result = self._tree(rows, [p])
        names = [c['name'] for c in result[0]['children']]
        self.assertEqual(names, ['Item1', 'item2', 'item10'])

    # ── Archive flag ──────────────────────────────────────────────────────────

    def test_archive_path_flagged(self):
        p = _FakePartition('p1')
        rows = [('arch.zip/inner.txt', 'p1'), ('plain/readme', 'p1')]
        archive_paths = {'arch.zip'}
        result = self._tree(rows, [p], archive_paths=archive_paths)
        children = {c['name']: c for c in result[0]['children']}
        self.assertTrue(children['arch.zip']['is_archive'])
        self.assertFalse(children['plain']['is_archive'])

    # ── Multi-partition ───────────────────────────────────────────────────────

    def test_multi_partition_separate_entries(self):
        p1 = _FakePartition('p1', index=0)
        p2 = _FakePartition('p2', index=1)
        rows = [('a/f', 'p1'), ('b/f', 'p2')]
        result = self._tree(rows, [p1, p2])
        self.assertEqual(len(result), 2)
        self.assertIs(result[0]['partition'], p1)
        self.assertIs(result[1]['partition'], p2)
        self.assertEqual(result[0]['children'][0]['name'], 'a')
        self.assertEqual(result[1]['children'][0]['name'], 'b')

    def test_multi_partition_empty_partition_skipped(self):
        p1 = _FakePartition('p1')
        p2 = _FakePartition('p2')
        rows = [('a/f', 'p1')]  # p2 gets no rows
        result = self._tree(rows, [p1, p2])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0]['partition'], p1)

    def test_partition_order_preserved(self):
        p1 = _FakePartition('p1', index=0)
        p2 = _FakePartition('p2', index=1)
        p3 = _FakePartition('p3', index=2)
        rows = [('x/f', 'p3'), ('y/f', 'p1'), ('z/f', 'p2')]
        result = self._tree(rows, [p1, p2, p3])
        uuids = [e['partition'].uuid for e in result]
        self.assertEqual(uuids, ['p1', 'p2', 'p3'])

    # ── Node structure ────────────────────────────────────────────────────────

    def test_node_path_ends_with_slash(self):
        p = _FakePartition('p1')
        rows = [('dir/file', 'p1')]
        result = self._tree(rows, [p])
        node = result[0]['children'][0]
        self.assertTrue(node['path'].endswith('/'))

    def test_node_is_archive_false_by_default(self):
        p = _FakePartition('p1')
        rows = [('dir/file', 'p1')]
        result = self._tree(rows, [p])
        node = result[0]['children'][0]
        self.assertFalse(node['is_archive'])

    def test_empty_dir_via_synthetic_slash_suffix(self):
        # Simulates the dirtree_html transform: is_directory rows have '/'
        # appended before being passed to build_directory_tree so that
        # _extract_dir_set creates the directory entry even with no files.
        p = _FakePartition('p1')
        rows = [('emptydir/', 'p1')]  # synthetic: path + '/'
        result = self._tree(rows, [p])
        names = [c['name'] for c in result[0]['children']]
        self.assertIn('emptydir', names)


if __name__ == '__main__':
    unittest.main()


# vim: ts=4 sw=4 et
