"""Tests for storage capacity reporting and deduplication statistics."""

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
os.environ.setdefault("SECRET_KEY", "ci-storage-stats-secret")
os.environ.setdefault("WORKER_API_KEY", "ci-test-worker-key")


class TestStorageStats(unittest.TestCase):
    def setUp(self):
        from arcology_shared.storage import create_storage
        from myapp.app import create_app
        from myapp.extensions import db

        self.tmpdir = tempfile.mkdtemp(prefix="arcology-storage-stats-")
        self.app = create_app()
        self.app.config.update({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "UPLOAD_FOLDER": os.path.join(self.tmpdir, "uploads"),
            "OUTPUT_FOLDER": os.path.join(self.tmpdir, "outputs"),
        })
        self.app.storage = create_storage(dict(self.app.config))
        self.client = self.app.test_client()
        self.db = db
        with self.app.app_context():
            db.create_all()
        # Reset the navbar TTL cache between tests.
        from myapp.services import storage_stats
        storage_stats._navbar_cache.update({"at": 0.0, "value": None})

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    def _make_user(self, username, permission=None, is_admin=False):
        from myapp.database import User, UserPermission
        user = User(username=username, is_admin=is_admin,
                    permission=permission or UserPermission.READ_WRITE)
        user.setPassword("a-long-test-password")
        self.db.session.add(user)
        self.db.session.flush()
        return user.id

    def _seed_shared_and_unique(self):
        """Two artefacts sharing one 100-byte upload blob, plus one unique 50-byte."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, Platform, StorageDirectory, UploadBlob
        from myapp.utils.blobs import assign_blob

        shared = b"x" * 100
        unique = b"y" * 50
        shared_sha = hashlib.sha256(shared).hexdigest()
        unique_sha = hashlib.sha256(unique).hexdigest()

        item = Item(name="Stats item", platform=Platform(name="Stats"))
        self.db.session.add(item)
        self.db.session.flush()

        for idx in range(2):
            art = Artefact(
                item_id=item.id, label=f"shared {idx}",
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename=f"shared-{idx}.img",
                storage_path=f"shared-{idx}.img",
                storage_directory=StorageDirectory.UPLOADS,
                file_size=len(shared), sha256=shared_sha,
            )
            self.db.session.add(art)
            assign_blob(art, StorageDirectory.UPLOADS, "shared.img",
                        len(shared), shared_sha,
                        logical_storage_path=f"shared-{idx}.img")

        unique_art = Artefact(
            item_id=item.id, label="unique",
            artefact_type=ArtefactType.RAW_SECTOR,
            original_filename="unique.img", storage_path="unique.img",
            storage_directory=StorageDirectory.UPLOADS,
            file_size=len(unique), sha256=unique_sha,
        )
        self.db.session.add(unique_art)
        assign_blob(unique_art, StorageDirectory.UPLOADS, "unique.img",
                    len(unique), unique_sha)
        self.db.session.commit()
        self.assertEqual(UploadBlob.query.count(), 2)

    def _seed_zero_length(self, count=3):
        """`count` artefacts that are all zero-length (sharing the empty SHA-256)."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, Platform, StorageDirectory

        empty_sha = hashlib.sha256(b"").hexdigest()
        item = Item(name="Empty item", platform=Platform(name="Empties"))
        self.db.session.add(item)
        self.db.session.flush()
        for idx in range(count):
            art = Artefact(
                item_id=item.id, label=f"empty {idx}",
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename=f"empty-{idx}.img",
                storage_path=f"empty-{idx}.img",
                storage_directory=StorageDirectory.UPLOADS,
                file_size=0, sha256=empty_sha,
            )
            self.db.session.add(art)
        self.db.session.commit()
        return empty_sha

    # -- deduplication statistics -------------------------------------------

    def test_dedup_stats_logical_physical_and_savings(self):
        from myapp.services.storage_stats import deduplication_stats
        with self.app.app_context():
            self._seed_shared_and_unique()
            stats = deduplication_stats()
            # Physical: one 100B blob + one 50B blob.
            self.assertEqual(stats["physical_bytes"], 150)
            self.assertEqual(stats["blob_count"], 2)
            # Logical: 100 counted twice (shared) + 50 once = 250.
            self.assertEqual(stats["logical_bytes"], 250)
            self.assertEqual(stats["saved_bytes"], 100)
            self.assertEqual(stats["shared_blob_count"], 1)
            self.assertAlmostEqual(stats["dedup_ratio"], 250 / 150, places=4)
            # One duplicated content group (the shared 100-byte content).
            self.assertEqual(len(stats["top_groups"]), 1)
            self.assertEqual(stats["top_groups"][0]["count"], 2)

    def test_dedup_stats_excludes_zero_length_artefacts(self):
        """Zero-length artefacts share the empty-file SHA-256 but waste no
        physical bytes, so they must not appear in the most-duplicated list."""
        from myapp.services.storage_stats import deduplication_stats
        with self.app.app_context():
            empty_sha = self._seed_zero_length(count=5)
            stats = deduplication_stats()
            self.assertEqual(stats["top_groups"], [])
            self.assertNotIn(
                empty_sha, {g["sha256"] for g in stats["top_groups"]})

    def test_dedup_stats_empty_collection(self):
        from myapp.services.storage_stats import deduplication_stats
        with self.app.app_context():
            stats = deduplication_stats()
            self.assertEqual(stats["physical_bytes"], 0)
            self.assertEqual(stats["logical_bytes"], 0)
            self.assertEqual(stats["saved_bytes"], 0)
            self.assertIsNone(stats["dedup_ratio"])
            self.assertEqual(stats["top_groups"], [])

    # -- capacity / disk usage ----------------------------------------------

    def test_local_backend_reports_disk_usage(self):
        usage = self.app.storage.disk_usage()
        self.assertIsNotNone(usage)
        self.assertEqual(usage["kind"], "local")
        self.assertGreater(usage["total"], 0)
        self.assertGreaterEqual(usage["free"], 0)
        self.assertLessEqual(usage["free"], usage["total"])

    def test_storage_capacity_local_kind(self):
        from myapp.services.storage_stats import storage_capacity
        with self.app.app_context():
            self._seed_shared_and_unique()
            cap = storage_capacity()
            self.assertEqual(cap["kind"], "local")
            self.assertGreater(cap["total"], 0)
            self.assertEqual(cap["arcology_bytes"], 150)

    def test_navbar_summary_local_has_label(self):
        from myapp.services.storage_stats import navbar_storage_summary
        with self.app.app_context():
            summary = navbar_storage_summary()
            self.assertIsNotNone(summary)
            self.assertEqual(summary["kind"], "local")
            self.assertIn("free", summary["label"])
            # Tooltip detail carries the breakdown rows.
            names = [name for name, _ in summary["detail"]]
            self.assertEqual(names, ["Collection", "Free", "Disk size"])

    def test_navbar_summary_s3_with_quota_reports_free(self):
        """Object store with a quota reports free space, not "X used"."""
        from myapp.services.storage_stats import navbar_storage_summary
        with self.app.app_context():
            self.app.storage.disk_usage = lambda: None
            self.app.config["STORAGE_CAPACITY_BYTES"] = 1000
            summary = navbar_storage_summary()
            self.assertEqual(summary["kind"], "s3")
            self.assertIn("free", summary["label"])
            names = [name for name, _ in summary["detail"]]
            self.assertEqual(names, ["Collection", "Free", "Quota"])

    def test_navbar_summary_s3_without_quota_falls_back_to_stored(self):
        """Object store with no quota has no total, so it reports stored bytes."""
        from myapp.services.storage_stats import navbar_storage_summary
        with self.app.app_context():
            self.app.storage.disk_usage = lambda: None
            summary = navbar_storage_summary()
            self.assertEqual(summary["kind"], "s3")
            self.assertIn("stored", summary["label"])
            self.assertEqual([n for n, _ in summary["detail"]], ["Collection"])

    def test_s3_disk_usage_is_none(self):
        from arcology_shared.storage import S3Storage
        # disk_usage is the inherited base default for object stores.
        self.assertIsNone(S3Storage.disk_usage(object.__new__(S3Storage)))

    # -- route access control -----------------------------------------------

    def test_storage_page_access_control(self):
        from myapp.database import UserPermission
        with self.app.app_context():
            staff_id = self._make_user("staff", UserPermission.STAFF)
            admin_id = self._make_user("admin", is_admin=True)
            ro_id = self._make_user("reader", UserPermission.READ_ONLY)
            rw_id = self._make_user("writer", UserPermission.READ_WRITE)
            self.db.session.commit()

        # Anonymous → redirect to login (not 200).
        self.assertNotEqual(self.client.get("/storage/").status_code, 200)

        for uid, expected in ((staff_id, 200), (admin_id, 200),
                              (rw_id, 403), (ro_id, 403)):
            with self.client.session_transaction() as sess:
                sess["_user_id"] = str(uid)
            resp = self.client.get("/storage/")
            self.assertEqual(resp.status_code, expected,
                             f"user {uid} expected {expected}, got {resp.status_code}")

    # -- object-store quota clamping ----------------------------------------

    def test_s3_capacity_clamps_when_quota_exceeded(self):
        """Over-quota object storage must not report negative free or >100%."""
        from myapp.services.storage_stats import storage_capacity
        with self.app.app_context():
            # Simulate an object store (no filesystem free-space figure) whose
            # stored content exceeds a small configured quota.
            self.app.storage.disk_usage = lambda: None
            self.app.config["STORAGE_CAPACITY_BYTES"] = 1000
            cap = storage_capacity(arcology_bytes=1500)
            self.assertEqual(cap["kind"], "s3")
            self.assertEqual(cap["used"], 1500)
            self.assertEqual(cap["free"], 0)            # clamped, not -500
            self.assertEqual(cap["percent_used"], 100.0)  # clamped, not 150.0

    def test_storage_capacity_used_is_footprint_for_local(self):
        """`used` is Arcology's footprint on every backend, not whole-disk."""
        from myapp.services.storage_stats import storage_capacity
        with self.app.app_context():
            self._seed_shared_and_unique()
            cap = storage_capacity()
            self.assertEqual(cap["kind"], "local")
            self.assertEqual(cap["used"], 150)
            self.assertEqual(cap["used"], cap["arcology_bytes"])

    def test_storage_capacity_reuses_precomputed_footprint(self):
        """A passed-in footprint is used verbatim (no blob re-scan)."""
        from myapp.services.storage_stats import storage_capacity
        with self.app.app_context():
            self.app.storage.disk_usage = lambda: None
            cap = storage_capacity(arcology_bytes=4242)
            self.assertEqual(cap["used"], 4242)
            self.assertEqual(cap["arcology_bytes"], 4242)


if __name__ == "__main__":
    unittest.main()
