"""
Tests for POST /api/artefacts/<uuid>/transform-to-disk-image — the worker-only
endpoint that turns a disk-image-bundle ZIP artefact into its disk image
in place (replacing the stored bytes, dropping the zip, queuing PARTITION_DETECT).
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-transform-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_AUTH = {'X-API-Key': os.environ['WORKER_API_KEY']}


class TestTransformToDiskImage(unittest.TestCase):
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

    def setUp(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            Item,
            OutputBlob,
            Platform,
            StorageDirectory,
            UploadBlob,
        )
        with self.app.app_context():
            from myapp.database import Analysis
            Analysis.query.delete()
            Artefact.query.delete()
            UploadBlob.query.delete()
            OutputBlob.query.delete()
            Item.query.delete()
            Platform.query.delete()
            self.db.session.commit()

            platform = Platform(name='P')
            self.db.session.add(platform)
            self.db.session.flush()
            item = Item(name='I', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()
            art = Artefact(
                item_id=item.id, label='bundle', artefact_type=ArtefactType.ZIP,
                original_filename='drive.zip', storage_path='oldzip.zip',
                storage_directory=StorageDirectory.UPLOADS,
            )
            self.db.session.add(art)
            self.db.session.commit()
            self.artefact_uuid = art.uuid

    def _payload(self, **over):
        p = {
            'analysis_id': 1,
            'storage_path': 'newimage.zst',
            'original_filename': 'drive.dd.zst',
            'artefact_type': 'raw_sector_zst',
            'file_size': 123, 'md5': 'a' * 32, 'sha256': 'b' * 64,
            'mime_type': 'application/zstd',
        }
        p.update(over)
        return p

    def _post(self, payload, auth=True):
        return self.client.post(
            f'/api/artefacts/{self.artefact_uuid}/transform-to-disk-image',
            json=payload, headers=_AUTH if auth else {},
        )

    def test_happy_path(self):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import Analysis, Artefact, StorageDirectory
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            self.assertEqual(art.artefact_type, ArtefactType.DD_ZST)
            self.assertEqual(art.storage_path, 'newimage.zst')
            self.assertEqual(art.storage_directory, StorageDirectory.UPLOADS)
            self.assertEqual(art.original_filename, 'drive.dd.zst')
            self.assertEqual(art.sha256, 'b' * 64)
            self.assertIsNone(art.parent_artefact_id)
            n = Analysis.query.filter_by(
                artefact_id=art.id, analysis_type=AnalysisType.PARTITION_DETECT).count()
            self.assertEqual(n, 1)

    def test_idempotent_retry(self):
        from myapp.database import Analysis, Artefact
        self.assertEqual(self._post(self._payload()).status_code, 200)
        resp2 = self._post(self._payload())
        self.assertEqual(resp2.status_code, 200, resp2.data)
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            self.assertEqual(art.storage_path, 'newimage.zst')
            # Still exactly one PARTITION_DETECT (no duplicate queued).
            from arcology_shared.enums import AnalysisType
            n = Analysis.query.filter_by(
                artefact_id=art.id, analysis_type=AnalysisType.PARTITION_DETECT).count()
            self.assertEqual(n, 1)

    def test_non_worker_blocked(self):
        self.assertNotEqual(self._post(self._payload(), auth=False).status_code, 200)

    def test_rejects_non_disk_image_type(self):
        resp = self._post(self._payload(artefact_type='zip'))
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_type_forced_even_when_overridden(self):
        # The stored bytes become a disk image, so the type MUST follow the
        # content even if a ZIP type was manually pinned — otherwise a stale ZIP
        # type would re-queue ARCHIVE_EXTRACT against a raw image.
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            art.type_overridden = True
            self.db.session.commit()
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            self.assertEqual(art.artefact_type, ArtefactType.DD_ZST)
            self.assertEqual(art.storage_path, 'newimage.zst')

    def test_duplicate_image_content_reuses_blob_without_merging_artefacts(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, StorageDirectory, UploadBlob
        from myapp.utils.blobs import assign_blob
        dup_sha = 'c' * 64
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            other = Artefact(
                item_id=art.item_id, label='pre-existing', artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='other.img', storage_path='other.img',
                storage_directory=StorageDirectory.UPLOADS,
            )
            assign_blob(
                other, StorageDirectory.UPLOADS, 'other.img',
                123, dup_sha, 'd' * 32,
            )
            self.db.session.add(other)
            self.db.session.commit()
            other_id = other.id
        resp = self._post(self._payload(sha256=dup_sha))
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            art = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            other = self.db.session.get(Artefact, other_id)
            self.assertEqual(art.artefact_type, ArtefactType.DD_ZST)
            self.assertNotEqual(art.id, other.id)
            self.assertEqual(art.upload_blob_id, other.upload_blob_id)
            self.assertEqual(UploadBlob.query.count(), 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
