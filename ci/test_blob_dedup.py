"""Tests for global upload/output blob deduplication."""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "ci-blob-dedup-secret")
os.environ.setdefault("WORKER_API_KEY", "ci-test-worker-key")


class TestBlobDedup(unittest.TestCase):
    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db
        from shared.storage import create_storage

        self.tmpdir = tempfile.mkdtemp(prefix="arcology-blob-dedup-")
        self.app = create_app()
        self.app.config.update({
            "TESTING": True,
            "UPLOAD_FOLDER": os.path.join(self.tmpdir, "uploads"),
            "OUTPUT_FOLDER": os.path.join(self.tmpdir, "outputs"),
        })
        self.app.storage = create_storage(dict(self.app.config))
        self.client = self.app.test_client()
        self.db = db
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_identical_uploads_are_distinct_artefacts_sharing_one_blob(self):
        from myapp.database import (
            Artefact,
            Item,
            Platform,
            StorageDirectory,
            UploadBlob,
            User,
        )
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        payload = b"same bytes"
        sha256 = hashlib.sha256(payload).hexdigest()
        md5 = hashlib.md5(payload).hexdigest()

        with self.app.app_context():
            platform = Platform(name="Blob test")
            item = Item(name="Shared item", platform=platform)
            users = [User(username="owner-a"), User(username="owner-b")]
            for user in users:
                user.setPassword("test password")
            self.db.session.add_all([platform, item, *users])
            self.db.session.flush()

            artefacts = []
            for index, owner in enumerate(users, start=1):
                storage_path = f"copy-{index}.img"
                key = self.app.storage.storage_key("uploads", storage_path)
                source = os.path.join(self.tmpdir, storage_path)
                with open(source, "wb") as stream:
                    stream.write(payload)
                self.app.storage.put(key, source)

                artefact = Artefact(
                    item_id=item.id,
                    label=f"Private copy {index}",
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=storage_path,
                    storage_path=storage_path,
                    storage_directory=StorageDirectory.UPLOADS,
                    owner_id=owner.id,
                    is_private=True,
                )
                blob, created = assign_blob(
                    artefact, StorageDirectory.UPLOADS, storage_path,
                    len(payload), sha256, md5,
                )
                if not created and blob.storage_path != storage_path:
                    self.app.storage.delete(key)
                self.db.session.add(artefact)
                artefacts.append(artefact)

            self.db.session.commit()

            self.assertEqual(Artefact.query.count(), 2)
            self.assertEqual(UploadBlob.query.count(), 1)
            self.assertEqual(artefacts[0].upload_blob_id, artefacts[1].upload_blob_id)
            self.assertNotEqual(artefacts[0].id, artefacts[1].id)
            self.assertNotEqual(artefacts[0].owner_id, artefacts[1].owner_id)
            self.assertTrue(artefacts[0].is_private)
            self.assertTrue(artefacts[1].is_private)
            canonical = artefacts[0].upload_blob.storage_path
            self.assertTrue(self.app.storage.exists(f"uploads/{canonical}"))
            self.assertFalse(self.app.storage.exists("uploads/copy-2.img"))

    def test_zero_length_blob_is_deduplicated_not_treated_as_unknown(self):
        from myapp.database import StorageDirectory, UploadBlob
        from myapp.utils.blobs import get_or_create_blob

        empty_sha256 = hashlib.sha256(b"").hexdigest()
        with self.app.app_context():
            first, created_first = get_or_create_blob(
                StorageDirectory.UPLOADS, "empty-a", 0, empty_sha256
            )
            second, created_second = get_or_create_blob(
                StorageDirectory.UPLOADS, "empty-b", 0, empty_sha256
            )
            self.db.session.commit()

            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first.id, second.id)
            self.assertEqual(UploadBlob.query.count(), 1)

    def test_output_blob_keeps_logical_lineage_path_and_physical_blob_path(self):
        from myapp.database import Artefact, Item, OutputBlob, Platform, StorageDirectory
        from myapp.utils.api_serializers import artefact_to_dict
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        payload = b"derived image"
        sha256 = hashlib.sha256(payload).hexdigest()
        with self.app.app_context():
            platform = Platform(name="Output blob test")
            item = Item(name="Output item", platform=platform)
            artefact = Artefact(
                item=item,
                label="Derived",
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename="derived.img",
                storage_path="derived/7/result",
                storage_directory=StorageDirectory.OUTPUTS,
            )
            assign_blob(
                artefact,
                StorageDirectory.OUTPUTS,
                f"blobs/{sha256[:2]}/{sha256}.img",
                len(payload),
                sha256,
                logical_storage_path="derived/7/result",
            )
            self.db.session.add_all([platform, item, artefact])
            self.db.session.commit()

            self.assertEqual(OutputBlob.query.count(), 1)
            self.assertEqual(artefact.storage_path, "derived/7/result")
            serialised = artefact_to_dict(artefact, include_storage=True)
            self.assertEqual(
                serialised["storage_path"],
                f"blobs/{sha256[:2]}/{sha256}.img",
            )

    def test_rehashing_output_blob_preserves_physical_and_logical_paths(self):
        from myapp.database import Artefact, Item, OutputBlob, Platform, StorageDirectory
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        old_sha256 = hashlib.sha256(b"incorrect hash").hexdigest()
        new_sha256 = hashlib.sha256(b"actual content").hexdigest()
        physical_path = f"blobs/{old_sha256[:2]}/{old_sha256}.img"
        logical_path = "derived/7/result"
        with self.app.app_context():
            platform = Platform(name="Rehash test")
            item = Item(name="Rehash item", platform=platform)
            artefact = Artefact(
                item=item,
                label="Derived",
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename="derived.img",
                storage_directory=StorageDirectory.OUTPUTS,
            )
            assign_blob(
                artefact,
                StorageDirectory.OUTPUTS,
                physical_path,
                14,
                old_sha256,
                logical_storage_path=logical_path,
            )
            self.db.session.add_all([platform, item, artefact])
            self.db.session.commit()
            artefact_uuid = artefact.uuid

        response = self.client.patch(
            f"/api/artefacts/{artefact_uuid}",
            headers={"X-API-Key": self.app.config["WORKER_API_KEY"]},
            json={"sha256": new_sha256},
        )
        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            artefact = Artefact.query.filter_by(uuid=artefact_uuid).one()
            self.assertEqual(artefact.storage_path, logical_path)
            self.assertEqual(artefact.output_blob.storage_path, physical_path)
            self.assertEqual(artefact.output_blob.sha256, new_sha256)
            self.assertEqual(OutputBlob.query.count(), 1)

    def test_rehashing_shared_blob_updates_all_artefact_hashes(self):
        from myapp.database import Artefact, Item, Platform, StorageDirectory
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        old_sha256 = hashlib.sha256(b"old metadata").hexdigest()
        new_sha256 = hashlib.sha256(b"shared bytes").hexdigest()
        with self.app.app_context():
            item = Item(name="Shared rehash item", platform=Platform(name="Shared rehash"))
            artefacts = []
            for index in range(2):
                artefact = Artefact(
                    item=item,
                    label=f"Copy {index}",
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=f"copy-{index}.img",
                    storage_directory=StorageDirectory.UPLOADS,
                )
                self.db.session.add(artefact)
                assign_blob(
                    artefact,
                    StorageDirectory.UPLOADS,
                    "shared.img",
                    12,
                    old_sha256,
                    logical_storage_path=f"copy-{index}.img",
                )
                artefacts.append(artefact)
            self.db.session.commit()

            assign_blob(
                artefacts[0],
                StorageDirectory.UPLOADS,
                "shared.img",
                12,
                new_sha256,
                logical_storage_path=artefacts[0].storage_path,
            )
            self.db.session.commit()

            self.assertEqual(artefacts[0].sha256, new_sha256)
            self.assertEqual(artefacts[1].sha256, new_sha256)
            self.assertEqual(artefacts[0].upload_blob_id, artefacts[1].upload_blob_id)

    def test_rehashing_converges_on_existing_blob_and_removes_obsolete_object(self):
        from myapp.database import Artefact, Item, Platform, StorageDirectory, UploadBlob
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        old_sha256 = hashlib.sha256(b"incorrect metadata").hexdigest()
        canonical_sha256 = hashlib.sha256(b"canonical bytes").hexdigest()
        with self.app.app_context():
            item = Item(name="Convergence item", platform=Platform(name="Convergence"))
            artefacts = []
            for label, storage_path, sha256 in (
                ("Canonical", "canonical.img", canonical_sha256),
                ("Incorrect A", "obsolete.img", old_sha256),
                ("Incorrect B", "obsolete.img", old_sha256),
            ):
                artefact = Artefact(
                    item=item,
                    label=label,
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=f"{label}.img",
                    storage_directory=StorageDirectory.UPLOADS,
                )
                self.db.session.add(artefact)
                assign_blob(
                    artefact,
                    StorageDirectory.UPLOADS,
                    storage_path,
                    15,
                    sha256,
                    logical_storage_path=f"logical/{label}",
                )
                artefacts.append(artefact)
            self.db.session.commit()
            corrected_uuid = artefacts[1].uuid
            artefact_ids = [artefact.id for artefact in artefacts]

            for storage_path in ("canonical.img", "obsolete.img"):
                source = os.path.join(self.tmpdir, storage_path)
                with open(source, "wb") as stream:
                    stream.write(b"canonical bytes")
                self.app.storage.put(f"uploads/{storage_path}", source)

        response = self.client.patch(
            f"/api/artefacts/{corrected_uuid}",
            headers={"X-API-Key": self.app.config["WORKER_API_KEY"]},
            json={"sha256": canonical_sha256},
        )
        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            refreshed = [self.db.session.get(Artefact, artefact_id) for artefact_id in artefact_ids]
            self.assertEqual(UploadBlob.query.count(), 1)
            self.assertEqual(len({artefact.upload_blob_id for artefact in refreshed}), 1)
            self.assertTrue(all(
                artefact.sha256 == canonical_sha256 for artefact in refreshed
            ))
            self.assertFalse(self.app.storage.exists("uploads/obsolete.img"))
            self.assertTrue(self.app.storage.exists("uploads/canonical.img"))

    def test_blob_object_is_deleted_only_after_last_reference(self):
        from myapp.blueprints.artefacts import _delete_artefact_files
        from myapp.database import Artefact, Item, Platform, StorageDirectory, UploadBlob
        from myapp.utils.blobs import assign_blob
        from shared.enums import ArtefactType

        payload = b"shared deletion content"
        sha256 = hashlib.sha256(payload).hexdigest()
        canonical = "canonical.img"
        with self.app.app_context():
            source = os.path.join(self.tmpdir, canonical)
            with open(source, "wb") as stream:
                stream.write(payload)
            self.app.storage.put(f"uploads/{canonical}", source)

            platform = Platform(name="Deletion test")
            item = Item(name="Deletion item", platform=platform)
            artefacts = []
            for index in range(2):
                artefact = Artefact(
                    item=item,
                    label=f"Copy {index}",
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=f"copy-{index}.img",
                    storage_path=f"copy-{index}.img",
                    storage_directory=StorageDirectory.UPLOADS,
                )
                self.db.session.add(artefact)
                assign_blob(
                    artefact, StorageDirectory.UPLOADS, canonical,
                    len(payload), sha256,
                    logical_storage_path=f"copy-{index}.img",
                )
                artefacts.append(artefact)
            self.db.session.add_all([platform, item])
            self.db.session.commit()

            _delete_artefact_files(artefacts[0])
            self.db.session.delete(artefacts[0])
            self.db.session.commit()
            self.assertTrue(self.app.storage.exists(f"uploads/{canonical}"))
            self.assertEqual(UploadBlob.query.count(), 1)

            remaining = artefacts[1]
            _delete_artefact_files(remaining)
            self.db.session.delete(remaining)
            self.db.session.commit()
            self.assertFalse(self.app.storage.exists(f"uploads/{canonical}"))
            self.assertEqual(UploadBlob.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
