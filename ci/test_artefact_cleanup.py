"""
Regression tests for artefact output cleanup.

Verifies that:
  - deleting a single artefact removes derived analysis outputs, generated
    output files, cache directories, and prunes the empty artefact directory
    even when Analysis.output_path was stored using the worker's absolute
    /data/outputs/... prefix
  - item-level cleanup uses the same shared cleanup path and removes outputs
    for worker-stored absolute output paths

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_artefact_cleanup -v
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
os.environ.setdefault('SECRET_KEY', 'ci-artefact-cleanup-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestArtefactCleanupRegression(unittest.TestCase):
    """Regression coverage for artefact/output cleanup paths."""

    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        from myapp.database import (
            User, UserPermission, Platform, Item, Artefact, Analysis,
            AnalysisStatus, StorageDirectory,
        )
        from shared.enums import ArtefactType, AnalysisType

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False

        self.tmpdir = tempfile.mkdtemp()
        self.upload_folder = os.path.join(self.tmpdir, 'uploads')
        self.output_folder = os.path.join(self.tmpdir, 'instance', 'outputs')
        self.app.config['UPLOAD_FOLDER'] = self.upload_folder
        self.app.config['OUTPUT_FOLDER'] = self.output_folder
        os.makedirs(self.upload_folder, exist_ok=True)
        os.makedirs(self.output_folder, exist_ok=True)

        self.client = self.app.test_client()
        self.db = _db

        with self.app.app_context():
            _db.create_all()

            user = User(
                username='cleanup-tester',
                permission=UserPermission.READ_WRITE,
                can_use_api=True,
            )
            user.setPassword('correct horse battery staple')
            _db.session.add(user)
            _db.session.flush()
            self.user_id = user.id

            platform = Platform(name='Cleanup Platform')
            _db.session.add(platform)
            _db.session.flush()

            item = Item(name='Cleanup Item', platform_id=platform.id)
            _db.session.add(item)
            _db.session.flush()
            self.item_id = item.id
            self.item_uuid = item.uuid

            storage_name = 'cleanup.img'
            self.storage_path = os.path.join(self.upload_folder, storage_name)
            with open(self.storage_path, 'w') as f:
                f.write('artefact')

            artefact = Artefact(
                item_id=item.id,
                label='Cleanup Disc',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='cleanup.img',
                storage_path=storage_name,
                storage_directory=StorageDirectory.UPLOADS,
            )
            _db.session.add(artefact)
            _db.session.flush()
            self.artefact_id = artefact.id
            self.artefact_uuid = artefact.uuid

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.FILE_EXTRACTION,
                status=AnalysisStatus.COMPLETED,
            )
            _db.session.add(analysis)
            _db.session.flush()

            item_dir = os.path.join(self.output_folder, f'{item.uuid}_{item.slug or "untitled"}')
            self.artefact_dir = os.path.join(
                item_dir, f'{artefact.uuid}_{artefact.slug or "untitled"}'
            )
            self.analysis_dir = os.path.join(
                self.artefact_dir, f'{analysis.uuid}_{analysis.slug or "untitled"}'
            )
            os.makedirs(self.analysis_dir, exist_ok=True)
            with open(os.path.join(self.analysis_dir, 'extracted.txt'), 'w') as f:
                f.write('derived output')

            # Keep the item directory non-empty so pruning stops after removing
            # the now-empty artefact directory.
            self.item_keep_file = os.path.join(item_dir, 'keep.txt')
            os.makedirs(item_dir, exist_ok=True)
            with open(self.item_keep_file, 'w') as f:
                f.write('keep')

            self.generated_output_rel = os.path.join(
                os.path.basename(item_dir),
                os.path.basename(self.artefact_dir),
                'preview.png',
            )
            self.generated_output_path = os.path.join(
                self.output_folder, self.generated_output_rel
            )
            with open(self.generated_output_path, 'w') as f:
                f.write('preview')

            self.cache_dir = os.path.join(self.output_folder, '.cache', artefact.uuid)
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(os.path.join(self.cache_dir, 'cache.bin'), 'w') as f:
                f.write('cache')

            worker_output_path = os.path.join(
                '/data/outputs',
                os.path.relpath(self.analysis_dir, self.output_folder),
            )
            analysis.output_path = worker_output_path
            analysis.details = json.dumps({
                'outputs': [{'filename': self.generated_output_rel}],
            })
            _db.session.commit()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True

    def test_delete_route_removes_outputs_and_prunes_artefact_dir(self):
        from myapp.database import Artefact

        self._login()
        resp = self.client.post(f'/artefacts/{self.artefact_uuid}/delete')
        self.assertEqual(resp.status_code, 302, resp.data)

        with self.app.app_context():
            self.assertIsNone(self.db.session.get(Artefact, self.artefact_id))

        self.assertFalse(os.path.exists(self.storage_path))
        self.assertFalse(os.path.exists(self.analysis_dir))
        self.assertFalse(os.path.exists(self.generated_output_path))
        self.assertFalse(os.path.exists(self.cache_dir))
        self.assertFalse(os.path.exists(self.artefact_dir))
        self.assertTrue(os.path.exists(self.item_keep_file))

    def test_delete_item_files_uses_same_cleanup_path(self):
        from myapp.blueprints.artefacts import _delete_item_files
        from myapp.database import Item

        with self.app.app_context():
            item = self.db.session.get(Item, self.item_id)
            _delete_item_files(item)

        self.assertFalse(os.path.exists(self.storage_path))
        self.assertFalse(os.path.exists(self.analysis_dir))
        self.assertFalse(os.path.exists(self.generated_output_path))
        self.assertFalse(os.path.exists(self.cache_dir))
        self.assertFalse(os.path.exists(self.artefact_dir))
        self.assertTrue(os.path.exists(self.item_keep_file))


if __name__ == '__main__':
    unittest.main()
