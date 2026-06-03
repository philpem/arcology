"""
Tests for the `arco bulk-import` file-discovery, recognition and
compressed-duplicate filtering logic (stdlib + cli package only).
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cli.arccli.commands.bulk_import import (  # noqa: E402
    _build_sidecar_bundle,
    _bundle_eligible,
    _dedupe_image_forms,
    _find_sidecars,
    _image_base,
    _is_importable,
    _matched_ext,
    discover_files,
    discover_files_flat,
)


def _entry(rel: str) -> dict:
    """Build a discovery-style file entry from a relative path."""
    p = Path(rel)
    return {'path': p, 'relative_path': rel, 'filename': p.name, 'label': rel}


def _names(entries: list[dict]) -> list[str]:
    return sorted(e['filename'] for e in entries)


class TestRecognition(unittest.TestCase):
    def test_raw_sector_images(self):
        for n in ('a.adf', 'a.img', 'a.ima', 'a.dsk', 'a.dd',
                  'a.raw', 'a.bin', 'a.hdd', 'a.hdf', 'a.image'):
            self.assertTrue(_is_importable(Path(n)), n)

    def test_compressed_raw_sector_variants(self):
        for n in ('a.dd.zst', 'a.dd.gz', 'a.dd.bz2',
                  'a.img.zst', 'a.adf.gz', 'a.raw.bz2'):
            self.assertTrue(_is_importable(Path(n)), n)

    def test_archives_including_7z(self):
        for n in ('a.zip', 'a.7z', 'a.rar', 'a.tgz', 'a.tar.gz'):
            self.assertTrue(_is_importable(Path(n)), n)

    def test_other_importable(self):
        for n in ('a.scp', 'a.imd', 'a.hfe', 'a.iso', 'a.pdf'):
            self.assertTrue(_is_importable(Path(n)), n)

    def test_non_importable(self):
        for n in ('a.map', 'a.txt', 'a.log', 'a.gz', 'a.zst', 'readme'):
            self.assertFalse(_is_importable(Path(n)), n)

    def test_case_insensitive(self):
        self.assertTrue(_is_importable(Path('A.DD.ZST')))
        self.assertEqual(_matched_ext('A.DD.ZST'), '.dd.zst')

    def test_image_base_strips_extension(self):
        self.assertEqual(_image_base('foo.dd.zst'), 'foo')
        self.assertEqual(_image_base('foo.dd'), 'foo')
        self.assertEqual(_image_base('foo.zip'), 'foo')
        self.assertEqual(_image_base('foo.tar.gz'), 'foo')


class TestDedupe(unittest.TestCase):
    def test_prefers_compressed_over_raw(self):
        kept, dropped = _dedupe_image_forms([_entry('c/foo.dd'),
                                             _entry('c/foo.dd.zst')])
        self.assertEqual(_names(kept), ['foo.dd.zst'])
        self.assertEqual(_names(dropped), ['foo.dd'])

    def test_prefers_archive_over_everything(self):
        kept, _ = _dedupe_image_forms([_entry('c/foo.dd'),
                                       _entry('c/foo.dd.zst'),
                                       _entry('c/foo.zip')])
        self.assertEqual(_names(kept), ['foo.zip'])

    def test_compressor_preference_zst_over_gz(self):
        kept, _ = _dedupe_image_forms([_entry('c/foo.dd.gz'),
                                       _entry('c/foo.dd.zst')])
        self.assertEqual(_names(kept), ['foo.dd.zst'])

    def test_different_directories_not_collapsed(self):
        kept, dropped = _dedupe_image_forms([_entry('a/foo.dd'),
                                             _entry('b/foo.dd.zst')])
        self.assertEqual(_names(kept), ['foo.dd', 'foo.dd.zst'])
        self.assertEqual(dropped, [])

    def test_different_raw_types_both_kept(self):
        kept, _ = _dedupe_image_forms([_entry('c/foo.dd'),
                                       _entry('c/foo.img.zst')])
        self.assertEqual(_names(kept), ['foo.dd', 'foo.img.zst'])

    def test_non_image_files_pass_through(self):
        # A pdf/iso sharing a base name is a different artefact, never dropped.
        kept, dropped = _dedupe_image_forms([_entry('c/foo.dd'),
                                             _entry('c/foo.dd.zst'),
                                             _entry('c/foo.pdf'),
                                             _entry('c/foo.iso')])
        self.assertEqual(_names(kept), ['foo.dd.zst', 'foo.iso', 'foo.pdf'])
        self.assertEqual(_names(dropped), ['foo.dd'])

    def test_lone_file_kept(self):
        kept, dropped = _dedupe_image_forms([_entry('c/foo.dd')])
        self.assertEqual(_names(kept), ['foo.dd'])
        self.assertEqual(dropped, [])


class TestDiscoverFiles(unittest.TestCase):
    def _tree(self, files: dict[str, bytes]):
        """Create a temp directory tree; returns the Path."""
        d = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, d)
        for rel, data in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return Path(d)

    @staticmethod
    def _rmtree(d):
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    def test_depth_one_files_grouped_under_archive_dir(self):
        # Files directly in the archive root must be imported, not skipped.
        root = self._tree({'foo1.dd.zst': b'x', 'foo2.dd.zst': b'x'})
        collections = discover_files(root, None, None)
        self.assertEqual(set(collections), {root.name})
        self.assertEqual(len(collections[root.name]), 2)

    def test_depth_two_grouped_by_top_dir(self):
        root = self._tree({'2025-01/foo.dd': b'x', 'XYZ/bar.dd.zst': b'x'})
        collections = discover_files(root, None, None)
        self.assertEqual(set(collections), {'2025-01', 'XYZ'})

    def test_dedupe_applied_in_discovery(self):
        root = self._tree({'2025-01/foo.dd': b'x', '2025-01/foo.dd.zst': b'x'})
        collections = discover_files(root, None, None)
        self.assertEqual(_names(collections['2025-01']), ['foo.dd.zst'])

    def test_dedupe_can_be_disabled(self):
        root = self._tree({'2025-01/foo.dd': b'x', '2025-01/foo.dd.zst': b'x'})
        collections = discover_files(root, None, None, dedupe=False)
        self.assertEqual(_names(collections['2025-01']), ['foo.dd', 'foo.dd.zst'])

    def test_flat_mode_dedupe(self):
        root = self._tree({'a/foo.dd': b'x', 'a/foo.zip': b'x', 'b/baz.img': b'x'})
        files = discover_files_flat(root, None)
        self.assertEqual(_names(files), ['baz.img', 'foo.zip'])


class TestSidecarBundling(unittest.TestCase):
    def _tree(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, d)
        for rel, data in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return Path(d)

    @staticmethod
    def _rmtree(d):
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    def test_bundle_eligible(self):
        self.assertTrue(_bundle_eligible('foo.dd'))
        self.assertTrue(_bundle_eligible('foo.dd.zst'))
        self.assertTrue(_bundle_eligible('foo.img'))
        self.assertFalse(_bundle_eligible('foo.zip'))   # archive
        self.assertFalse(_bundle_eligible('foo.iso'))   # non-image
        self.assertFalse(_bundle_eligible('foo.pdf'))

    def test_find_sidecars_base_name_and_generic(self):
        root = self._tree({
            'foo.dd.zst': b'x', 'foo.map': b'x', 'foo.log': b'x',
            'foo.txt': b'x',             # per-drive readme sharing the base name
            'README.txt': b'x', 'SHA256SUMS': b'x', 'notes.md5': b'x',
            'bar.dd.zst': b'x',          # a different image — not a sidecar
            'foo.dd': b'x',              # importable — not a sidecar
        })
        img = root / 'foo.dd.zst'
        names = sorted(p.name for p in _find_sidecars(img, 'foo'))
        self.assertEqual(
            names,
            ['README.txt', 'SHA256SUMS', 'foo.log', 'foo.map', 'foo.txt', 'notes.md5'])

    def test_find_sidecars_per_drive_readme_with_image_name(self):
        # A readme named after the full image (harddrive.dd.txt) is bundled.
        root = self._tree({'harddrive.dd.zst': b'x', 'harddrive.dd.txt': b'x'})
        names = [p.name for p in _find_sidecars(root / 'harddrive.dd.zst', 'harddrive')]
        self.assertEqual(names, ['harddrive.dd.txt'])

    def test_find_sidecars_excludes_other_images(self):
        root = self._tree({'foo.dd.zst': b'x', 'bar.img': b'x', 'foo.map': b'x'})
        names = [p.name for p in _find_sidecars(root / 'foo.dd.zst', 'foo')]
        self.assertEqual(names, ['foo.map'])

    def test_find_sidecars_none(self):
        root = self._tree({'foo.dd.zst': b'x'})
        self.assertEqual(_find_sidecars(root / 'foo.dd.zst', 'foo'), [])

    def test_build_bundle_stores_image_deflates_sidecars(self):
        root = self._tree({'foo.dd.zst': b'COMPRESSED' * 100,
                           'foo.map': b'text ' * 100})
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _build_sidecar_bundle(
                root / 'foo.dd.zst', [root / 'foo.map'], 'foo', tmp)
            self.assertEqual(zip_path.name, 'foo.zip')
            with zipfile.ZipFile(zip_path) as zf:
                info = {zi.filename: zi for zi in zf.infolist()}
                self.assertEqual(set(info), {'foo.dd.zst', 'foo.map'})
                self.assertEqual(info['foo.dd.zst'].compress_type,
                                 zipfile.ZIP_STORED)
                self.assertEqual(info['foo.map'].compress_type,
                                 zipfile.ZIP_DEFLATED)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
