"""
Tests for the storage backend abstraction (shared/storage.py).

Covers LocalStorage operations, path traversal protection, create_storage
factory, and storage_key building.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_storage -v
"""

import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestLocalStorageBasicOps(unittest.TestCase):
    """Test LocalStorage put/get/delete/exists operations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.uploads = os.path.join(self.tmpdir, 'uploads')
        self.outputs = os.path.join(self.tmpdir, 'outputs')
        from shared.storage import LocalStorage
        self.storage = LocalStorage(self.uploads, self.outputs)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _write_temp(self, content=b'hello world'):
        fd, path = tempfile.mkstemp()
        os.write(fd, content)
        os.close(fd)
        return path

    def test_put_and_exists(self):
        src = self._write_temp(b'test data')
        self.storage.put('uploads/test.img', src)
        os.unlink(src)
        self.assertTrue(self.storage.exists('uploads/test.img'))
        self.assertFalse(self.storage.exists('uploads/nonexistent.img'))

    def test_put_and_get(self):
        content = b'test content 12345'
        src = self._write_temp(content)
        self.storage.put('uploads/file.bin', src)
        os.unlink(src)

        dest = os.path.join(self.tmpdir, 'downloaded.bin')
        self.storage.get('uploads/file.bin', dest)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), content)

    def test_put_creates_subdirectories(self):
        src = self._write_temp()
        self.storage.put('outputs/item/art/analysis/file.txt', src)
        os.unlink(src)
        self.assertTrue(self.storage.exists('outputs/item/art/analysis/file.txt'))

    def test_open_read(self):
        content = b'readable content'
        src = self._write_temp(content)
        self.storage.put('uploads/readable.bin', src)
        os.unlink(src)

        f = self.storage.open_read('uploads/readable.bin')
        try:
            self.assertEqual(f.read(), content)
        finally:
            f.close()

    def test_delete(self):
        src = self._write_temp()
        self.storage.put('uploads/to_delete.bin', src)
        os.unlink(src)
        self.assertTrue(self.storage.exists('uploads/to_delete.bin'))
        self.storage.delete('uploads/to_delete.bin')
        self.assertFalse(self.storage.exists('uploads/to_delete.bin'))

    def test_delete_nonexistent_no_error(self):
        # Should not raise
        self.storage.delete('uploads/nonexistent.bin')

    def test_delete_prefix_directory(self):
        src = self._write_temp()
        self.storage.put('outputs/dir/a.txt', src)
        self.storage.put('outputs/dir/sub/b.txt', src)
        self.storage.put('outputs/other/c.txt', src)
        os.unlink(src)

        deleted = self.storage.delete_prefix('outputs/dir')
        self.assertEqual(deleted, 2)
        self.assertFalse(self.storage.exists('outputs/dir/a.txt'))
        self.assertFalse(self.storage.exists('outputs/dir/sub/b.txt'))
        self.assertTrue(self.storage.exists('outputs/other/c.txt'))

    def test_delete_prefix_nonexistent(self):
        self.assertEqual(self.storage.delete_prefix('outputs/gone'), 0)

    def test_list_prefix(self):
        src = self._write_temp()
        self.storage.put('outputs/tree/a.txt', src)
        self.storage.put('outputs/tree/sub/b.txt', src)
        self.storage.put('outputs/other/c.txt', src)
        os.unlink(src)

        keys = sorted(self.storage.list_prefix('outputs/tree'))
        self.assertEqual(keys, ['outputs/tree/a.txt', 'outputs/tree/sub/b.txt'])

    def test_list_prefix_empty(self):
        self.assertEqual(self.storage.list_prefix('outputs/missing'), [])

    def test_presigned_url_returns_none(self):
        self.assertIsNone(self.storage.presigned_url('uploads/x.bin'))

    def test_local_path(self):
        path = self.storage.local_path('uploads/test.img')
        expected = os.path.join(self.uploads, 'test.img')
        self.assertEqual(str(path), os.path.realpath(expected))

    def test_put_same_file_noop(self):
        """put() is a no-op when source and dest resolve to the same file."""
        src = self._write_temp(b'original')
        key = 'uploads/' + os.path.basename(src)
        # Place file directly in uploads
        dest = self.storage.local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        os.unlink(src)

        # put with the same path should not error
        self.storage.put(key, dest)
        self.assertTrue(self.storage.exists(key))


class TestLocalStorageTreeOps(unittest.TestCase):
    """Test put_tree and get_tree operations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.uploads = os.path.join(self.tmpdir, 'uploads')
        self.outputs = os.path.join(self.tmpdir, 'outputs')
        from shared.storage import LocalStorage
        self.storage = LocalStorage(self.uploads, self.outputs)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _create_tree(self, base_dir):
        """Create a small directory tree and return file count."""
        os.makedirs(os.path.join(base_dir, 'sub'), exist_ok=True)
        for name in ('a.txt', 'b.txt', 'sub/c.txt'):
            with open(os.path.join(base_dir, name), 'w') as f:
                f.write(f'content of {name}')
        return 3

    def test_put_tree(self):
        src_dir = os.path.join(self.tmpdir, 'source_tree')
        os.makedirs(src_dir)
        expected_count = self._create_tree(src_dir)

        count = self.storage.put_tree('outputs/extraction', src_dir)
        self.assertEqual(count, expected_count)
        self.assertTrue(self.storage.exists('outputs/extraction/a.txt'))
        self.assertTrue(self.storage.exists('outputs/extraction/sub/c.txt'))

    def test_get_tree(self):
        # First put a tree
        src_dir = os.path.join(self.tmpdir, 'source_tree')
        os.makedirs(src_dir)
        self._create_tree(src_dir)
        self.storage.put_tree('outputs/extraction', src_dir)

        # Now get it back
        dest_dir = os.path.join(self.tmpdir, 'dest_tree')
        count = self.storage.get_tree('outputs/extraction', dest_dir)
        self.assertEqual(count, 3)
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, 'a.txt')))
        self.assertTrue(os.path.isfile(os.path.join(dest_dir, 'sub', 'c.txt')))

    def test_get_tree_nonexistent(self):
        dest_dir = os.path.join(self.tmpdir, 'empty_dest')
        count = self.storage.get_tree('outputs/missing', dest_dir)
        self.assertEqual(count, 0)

    def test_put_tree_replaces_existing(self):
        """put_tree should replace an existing directory."""
        src1 = os.path.join(self.tmpdir, 'tree1')
        os.makedirs(src1)
        with open(os.path.join(src1, 'old.txt'), 'w') as f:
            f.write('old')
        self.storage.put_tree('outputs/replaced', src1)
        self.assertTrue(self.storage.exists('outputs/replaced/old.txt'))

        src2 = os.path.join(self.tmpdir, 'tree2')
        os.makedirs(src2)
        with open(os.path.join(src2, 'new.txt'), 'w') as f:
            f.write('new')
        self.storage.put_tree('outputs/replaced', src2)

        self.assertTrue(self.storage.exists('outputs/replaced/new.txt'))
        self.assertFalse(self.storage.exists('outputs/replaced/old.txt'))


class TestLocalStoragePathTraversal(unittest.TestCase):
    """Verify that path traversal attacks are blocked."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.uploads = os.path.join(self.tmpdir, 'uploads')
        self.outputs = os.path.join(self.tmpdir, 'outputs')
        from shared.storage import LocalStorage
        self.storage = LocalStorage(self.uploads, self.outputs)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_dotdot_traversal_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.storage._resolve('uploads/../../../etc/passwd')
        self.assertIn('traversal', str(ctx.exception).lower())

    def test_deep_dotdot_traversal_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.storage._resolve('outputs/a/b/../../../../etc/shadow')
        self.assertIn('traversal', str(ctx.exception).lower())

    def test_dotdot_in_put_raises(self):
        fd, src = tempfile.mkstemp()
        os.write(fd, b'malicious')
        os.close(fd)
        try:
            with self.assertRaises(ValueError):
                self.storage.put('uploads/../outside.txt', src)
        finally:
            os.unlink(src)

    def test_dotdot_in_get_raises(self):
        with self.assertRaises(ValueError):
            self.storage.get('uploads/../../etc/passwd', '/tmp/stolen')

    def test_dotdot_in_exists_raises(self):
        with self.assertRaises(ValueError):
            self.storage.exists('uploads/../../../etc/passwd')

    def test_dotdot_in_delete_raises(self):
        with self.assertRaises(ValueError):
            self.storage.delete('outputs/../../../etc/important')

    def test_valid_key_resolves(self):
        """Normal keys without traversal should work fine."""
        path = self.storage._resolve('uploads/abc123.img')
        self.assertTrue(str(path).endswith('abc123.img'))

    def test_valid_nested_key_resolves(self):
        path = self.storage._resolve('outputs/item/art/analysis/file.txt')
        self.assertTrue(str(path).endswith('file.txt'))

    def test_invalid_prefix_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.storage._resolve('invalid/key.txt')
        self.assertIn('uploads/', str(ctx.exception))


class TestStorageKey(unittest.TestCase):
    """Test storage_key method on StorageBackend."""

    def test_uploads_key(self):
        from shared.storage import LocalStorage
        storage = LocalStorage(tempfile.mkdtemp(), tempfile.mkdtemp())
        self.assertEqual(
            storage.storage_key('uploads', 'abc123.img'),
            'uploads/abc123.img'
        )

    def test_outputs_key(self):
        from shared.storage import LocalStorage
        storage = LocalStorage(tempfile.mkdtemp(), tempfile.mkdtemp())
        self.assertEqual(
            storage.storage_key('outputs', 'subdir/file.png'),
            'outputs/subdir/file.png'
        )


class TestCreateStorage(unittest.TestCase):
    """Test the create_storage factory function."""

    def test_default_creates_local(self):
        from shared.storage import LocalStorage, create_storage
        tmpdir = tempfile.mkdtemp()
        try:
            storage = create_storage({
                'UPLOAD_FOLDER': os.path.join(tmpdir, 'u'),
                'OUTPUT_FOLDER': os.path.join(tmpdir, 'o'),
            })
            self.assertIsInstance(storage, LocalStorage)
        finally:
            shutil.rmtree(tmpdir)

    def test_explicit_local_creates_local(self):
        from shared.storage import LocalStorage, create_storage
        tmpdir = tempfile.mkdtemp()
        try:
            storage = create_storage({
                'STORAGE_BACKEND': 'local',
                'UPLOAD_FOLDER': os.path.join(tmpdir, 'u'),
                'OUTPUT_FOLDER': os.path.join(tmpdir, 'o'),
            })
            self.assertIsInstance(storage, LocalStorage)
        finally:
            shutil.rmtree(tmpdir)

    def test_invalid_backend_raises(self):
        from shared.storage import create_storage
        with self.assertRaises(ValueError) as ctx:
            create_storage({'STORAGE_BACKEND': 'gcs'})
        self.assertIn('gcs', str(ctx.exception))

    def test_s3_missing_credentials_raises(self):
        from shared.storage import create_storage
        with self.assertRaises(RuntimeError) as ctx:
            create_storage({
                'STORAGE_BACKEND': 's3',
                'S3_ENDPOINT_URL': 'http://localhost:3900',
                # Missing S3_ACCESS_KEY and S3_SECRET_KEY
            })
        self.assertIn('S3_ACCESS_KEY', str(ctx.exception))

    def test_case_insensitive_backend(self):
        from shared.storage import LocalStorage, create_storage
        tmpdir = tempfile.mkdtemp()
        try:
            storage = create_storage({
                'STORAGE_BACKEND': 'LOCAL',
                'UPLOAD_FOLDER': os.path.join(tmpdir, 'u'),
                'OUTPUT_FOLDER': os.path.join(tmpdir, 'o'),
            })
            self.assertIsInstance(storage, LocalStorage)
        finally:
            shutil.rmtree(tmpdir)


class TestStorageIntegrationWithApp(unittest.TestCase):
    """Verify that the Flask app initialises the storage backend correctly."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
        os.environ.setdefault('SECRET_KEY', 'ci-storage-test-key')
        os.environ.setdefault('WORKER_API_KEY', 'test')

    def test_app_has_storage_attribute(self):
        from myapp.app import create_app
        from shared.storage import LocalStorage
        app = create_app()
        self.assertTrue(hasattr(app, 'storage'))
        self.assertIsInstance(app.storage, LocalStorage)

    def test_get_artefact_storage_key(self):
        """get_artefact_storage_key builds the correct storage key."""
        from myapp.app import create_app
        from myapp.database import Artefact, Item, Platform, StorageDirectory
        from myapp.extensions import db
        from shared.enums import ArtefactType

        app = create_app()
        app.config['TESTING'] = True

        with app.app_context():
            db.create_all()

            platform = Platform(name='Test Platform')
            db.session.add(platform)
            db.session.flush()

            item = Item(name='Test Item', platform_id=platform.id)
            db.session.add(item)
            db.session.flush()

            artefact = Artefact(
                item_id=item.id,
                label='Test',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test.img',
                storage_path='abc123.img',
                storage_directory=StorageDirectory.UPLOADS,
            )
            db.session.add(artefact)
            db.session.flush()

            from myapp.blueprints.artefacts import get_artefact_storage_key
            key = get_artefact_storage_key(artefact)
            self.assertEqual(key, 'uploads/abc123.img')

            # Test with outputs directory
            artefact2 = Artefact(
                item_id=item.id,
                label='Derived',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='derived.img',
                storage_path='derived123.img',
                storage_directory=StorageDirectory.OUTPUTS,
            )
            db.session.add(artefact2)
            db.session.flush()

            key2 = get_artefact_storage_key(artefact2)
            self.assertEqual(key2, 'outputs/derived123.img')

            db.session.rollback()

    def test_compute_file_hashes_via_storage(self):
        """compute_file_hashes with use_storage=True reads from the backend."""
        import hashlib
        from myapp.app import create_app

        app = create_app()
        app.config['TESTING'] = True
        tmpdir = tempfile.mkdtemp()
        app.config['UPLOAD_FOLDER'] = os.path.join(tmpdir, 'uploads')
        app.config['OUTPUT_FOLDER'] = os.path.join(tmpdir, 'outputs')

        from shared.storage import create_storage
        app.storage = create_storage({
            'UPLOAD_FOLDER': app.config['UPLOAD_FOLDER'],
            'OUTPUT_FOLDER': app.config['OUTPUT_FOLDER'],
        })

        with app.app_context():
            # Write a test file into storage
            content = b'hash me please'
            fd, src = tempfile.mkstemp()
            os.write(fd, content)
            os.close(fd)
            app.storage.put('uploads/hashtest.bin', src)
            os.unlink(src)

            from myapp.blueprints.artefacts import compute_file_hashes
            md5, sha256 = compute_file_hashes('uploads/hashtest.bin', use_storage=True)

            expected_md5 = hashlib.md5(content).hexdigest()
            expected_sha256 = hashlib.sha256(content).hexdigest()
            self.assertEqual(md5, expected_md5)
            self.assertEqual(sha256, expected_sha256)

        shutil.rmtree(tmpdir)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
