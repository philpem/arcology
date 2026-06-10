"""
Regression tests for path traversal vulnerabilities (#2, #3, #4).

Verifies that:
  - POST /api/items/<uuid>/artefacts rejects absolute storage_path values (#2)
  - POST /api/items/<uuid>/artefacts rejects ../ traversal in storage_path (#2)
  - POST /api/items/<uuid>/artefacts accepts valid relative storage_path (#2 non-regression)
  - POST /api/analysis/<id>/produce-artefact rejects absolute storage_path (#2)
  - POST /api/analysis/<id>/produce-artefact rejects ../ traversal in storage_path (#2)
  - get_artefact_path() raises ValueError for absolute storage_path (#2 layer 1)
  - get_artefact_path() raises ValueError for ../ traversal storage_path (#2 layer 1)
  - resolve_extracted_file_path() returns None for ../ traversal in ef.path (#3)
  - _delete_item_files() skips output files with ../ traversal in filename (#4)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_path_traversal -v
"""

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-path-traversal-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_AUTH = {'X-API-Key': _WORKER_KEY}


def _make_fixtures(db):
    """Create Platform -> Item -> Artefact fixture. Returns (item, artefact)."""
    from myapp.database import Artefact, Item, Platform
    from shared.enums import ArtefactType

    platform = Platform(name='Test Platform')
    db.session.add(platform)
    db.session.flush()

    item = Item(name='Test Item', platform_id=platform.id)
    db.session.add(item)
    db.session.flush()

    artefact = Artefact(
        item_id=item.id,
        label='Test Disc',
        artefact_type=ArtefactType.RAW_SECTOR,
        original_filename='test.img',
        storage_path='test.img',
    )
    db.session.add(artefact)
    db.session.commit()
    return item, artefact


# =============================================================================
# API input validation — add_artefact (POST /api/items/<uuid>/artefacts)
# =============================================================================

class TestAddArtefactStoragePathValidation(unittest.TestCase):
    """POST /api/items/<uuid>/artefacts must reject dangerous storage_path values."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()
            cls.item, _ = _make_fixtures(_db)
            cls.item_uuid = cls.item.uuid

    def _post(self, storage_path):
        return self.client.post(
            f'/api/items/{self.item_uuid}/artefacts',
            json={
                'label': 'Traversal Test',
                'original_filename': 'test.img',
                'storage_path': storage_path,
                'artefact_type': 'raw_sector',
            },
            headers=_AUTH,
        )

    def test_absolute_path_rejected(self):
        resp = self._post('/etc/passwd')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_absolute_tmp_path_rejected(self):
        resp = self._post('/tmp/secret.txt')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_dotdot_traversal_rejected(self):
        resp = self._post('../../etc/passwd')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_dotdot_simple_rejected(self):
        resp = self._post('../outside.txt')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_valid_relative_path_accepted(self):
        resp = self._post('abc123.img')
        self.assertNotEqual(resp.status_code, 400, resp.data)

    def test_valid_subdirectory_path_accepted(self):
        resp = self._post('subdir/abc123.img')
        self.assertNotEqual(resp.status_code, 400, resp.data)


# =============================================================================
# API input validation — report_derived_artefact
# =============================================================================

class TestProduceArtefactStoragePathValidation(unittest.TestCase):
    """POST /api/analysis/<id>/produce-artefact must reject dangerous storage_path."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()
            from myapp.database import Analysis, AnalysisStatus, Artefact, Item, Platform
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='Test Platform 2')
            _db.session.add(platform)
            _db.session.flush()

            item = Item(name='Test Item 2', platform_id=platform.id)
            _db.session.add(item)
            _db.session.flush()

            artefact = Artefact(
                item_id=item.id,
                label='Test Disc 2',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test2.img',
                storage_path='test2.img',
            )
            _db.session.add(artefact)
            _db.session.flush()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.FILE_EXTRACTION,
                status=AnalysisStatus.RUNNING,
            )
            _db.session.add(analysis)
            _db.session.commit()
            cls.analysis_id = analysis.id

    def _post(self, storage_path):
        return self.client.post(
            f'/api/analysis/{self.analysis_id}/produce-artefact',
            json={
                'label': 'Derived',
                'original_filename': 'derived.img',
                'storage_path': storage_path,
                'artefact_type': 'raw_sector',
            },
            headers=_AUTH,
        )

    def test_absolute_path_rejected(self):
        resp = self._post('/etc/passwd')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_dotdot_traversal_rejected(self):
        resp = self._post('../outside.txt')
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_valid_path_accepted(self):
        resp = self._post('derived_abc123.img')
        self.assertIn(resp.status_code, (200, 201), resp.data)


# =============================================================================
# get_artefact_path() confinement check (layer 1)
# =============================================================================

class TestGetArtefactPathConfinement(unittest.TestCase):
    """get_artefact_path() must raise ValueError for any escaping storage_path."""

    def setUp(self):
        from myapp.app import create_app
        self.app = create_app()
        self.tmpdir = tempfile.mkdtemp()
        self.app.config['UPLOAD_FOLDER'] = os.path.join(self.tmpdir, 'uploads')
        self.app.config['OUTPUT_FOLDER'] = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(self.app.config['UPLOAD_FOLDER'], exist_ok=True)
        os.makedirs(self.app.config['OUTPUT_FOLDER'], exist_ok=True)

    def _make_artefact(self, storage_path):
        from unittest.mock import MagicMock
        from myapp.database import StorageDirectory
        a = MagicMock()
        a.storage_path = storage_path
        a.storage_directory = StorageDirectory.UPLOADS
        return a

    def _call(self, storage_path):
        from myapp.services.artefact_storage import get_artefact_path
        with self.app.app_context():
            return get_artefact_path(self._make_artefact(storage_path))

    def test_absolute_path_raises(self):
        with self.assertRaises(ValueError):
            self._call('/etc/passwd')

    def test_dotdot_traversal_raises(self):
        with self.assertRaises(ValueError):
            self._call('../outside.txt')

    def test_deep_dotdot_traversal_raises(self):
        with self.assertRaises(ValueError):
            self._call('sub/../../outside.txt')

    def test_valid_path_returns_path(self):
        result = self._call('abc123.img')
        self.assertTrue(result.startswith(self.app.config['UPLOAD_FOLDER']))

    def test_valid_subdirectory_path_returns_path(self):
        result = self._call('sub/abc123.img')
        self.assertTrue(result.startswith(self.app.config['UPLOAD_FOLDER']))


# =============================================================================
# resolve_extracted_file_path() confinement check (#3)
# =============================================================================

class TestResolveExtractedFilePathConfinement(unittest.TestCase):
    """resolve_extracted_file_path() must return None for ../ in ef.path."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.tmpdir = tempfile.mkdtemp()
        self.app.config['OUTPUT_FOLDER'] = os.path.join(self.tmpdir, 'outputs')

        extract_dir = os.path.join(self.app.config['OUTPUT_FOLDER'], 'extract-job')
        os.makedirs(extract_dir, exist_ok=True)

        # Place a secret file one level above the extraction dir
        self.secret = os.path.join(self.app.config['OUTPUT_FOLDER'], 'escaped-secret.txt')
        with open(self.secret, 'w') as f:
            f.write('EXTRACT_ESCAPE_POC')

        with self.app.app_context():
            _db.create_all()
            from myapp.database import (
                Analysis,
                AnalysisStatus,
                Artefact,
                ExtractedFile,
                FilesystemType,
                Item,
                Partition,
                Platform,
                StorageDirectory,
            )
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='Traverse Platform')
            _db.session.add(platform)
            _db.session.flush()
            item = Item(name='Traverse Item', platform_id=platform.id)
            _db.session.add(item)
            _db.session.flush()
            artefact = Artefact(
                item_id=item.id, label='Traverse Disc',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='t.img', storage_path='t.img',
                storage_directory=StorageDirectory.UPLOADS,
            )
            _db.session.add(artefact)
            _db.session.flush()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.FILE_EXTRACTION,
                status=AnalysisStatus.COMPLETED,
                output_path=extract_dir,
            )
            _db.session.add(analysis)
            _db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                partition_index=0,
                label='Root',
                filesystem=FilesystemType.FAT12,
            )
            _db.session.add(partition)
            _db.session.flush()

            self.ef = ExtractedFile(
                partition_id=partition.id,
                filename='escaped-secret.txt',
                path='../escaped-secret.txt',
            )
            _db.session.add(self.ef)
            _db.session.commit()
            self.ef_id = self.ef.id

    def test_traversal_path_returns_none(self):
        from myapp.database import ExtractedFile
        from myapp.services.artefact_storage import resolve_extracted_file_path
        with self.app.app_context():
            ef = ExtractedFile.query.get(self.ef_id)
            result = resolve_extracted_file_path(ef)
        self.assertIsNone(result, f"Expected None but got {result!r}")

    def test_secret_file_not_served(self):
        """The escaped file must still exist on disk (not deleted, just not served)."""
        self.assertTrue(os.path.exists(self.secret))


# =============================================================================
# _delete_item_files() output file confinement (#4)
# =============================================================================

class TestDeleteItemFilesConfinement(unittest.TestCase):
    """_delete_item_files() must not delete files outside output_folder."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.tmpdir = tempfile.mkdtemp()
        upload_folder = os.path.join(self.tmpdir, 'uploads')
        self.app.config['UPLOAD_FOLDER'] = upload_folder
        self.app.config['OUTPUT_FOLDER'] = os.path.join(self.tmpdir, 'outputs')
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(self.app.config['OUTPUT_FOLDER'], exist_ok=True)

        # The file that must NOT be deleted
        self.outside_file = os.path.join(self.tmpdir, 'outside.txt')
        with open(self.outside_file, 'w') as f:
            f.write('SHOULD_NOT_BE_DELETED')

        with self.app.app_context():
            _db.create_all()
            from myapp.database import Analysis, AnalysisStatus, Artefact, Item, Platform, StorageDirectory
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='Delete Platform')
            _db.session.add(platform)
            _db.session.flush()
            item = Item(name='Delete Item', platform_id=platform.id)
            _db.session.add(item)
            _db.session.flush()

            # Artefact storage file (needs to exist for _delete_artefact_files)
            storage_name = 'stored.img'
            storage_full = os.path.join(upload_folder, storage_name)
            with open(storage_full, 'w') as f:
                f.write('artefact')

            artefact = Artefact(
                item_id=item.id, label='Delete Disc',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='d.img', storage_path=storage_name,
                storage_directory=StorageDirectory.UPLOADS,
            )
            _db.session.add(artefact)
            _db.session.flush()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.FILE_EXTRACTION,
                status=AnalysisStatus.COMPLETED,
                details=json.dumps({'outputs': [{'filename': '../outside.txt'}]}),
            )
            _db.session.add(analysis)
            _db.session.commit()
            self.item_id = item.id

    def test_outside_file_not_deleted(self):
        from myapp.blueprints.artefacts import _delete_item_files
        from myapp.database import Item
        with self.app.app_context():
            item = Item.query.get(self.item_id)
            _delete_item_files(item)
        self.assertTrue(
            os.path.exists(self.outside_file),
            'File outside output_folder was deleted — path traversal not blocked',
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
