"""
Download restriction and multi-badge hash display tests.

Covers:
  - ArtefactRestriction model CRUD and unique constraint
  - UserRestrictionBypass and can_bypass_restriction()/can_bypass_all_restrictions()
  - Download returns 403 when restricted (API)
  - artefact_to_dict includes restriction fields
  - find_all_known_files_batch returns multiple matches
  - apply_database_restrictions auto-adds from flagged HashDBs
  - ArtefactRestriction cascades on artefact delete

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_restrictions -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-restrictions-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


def _enable_sqlite_fks(app, _db):
    from sqlalchemy import event
    @event.listens_for(_db.engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _enable_sqlite_fks(app, _db)
        _db.create_all()
    return app, _db


_counter = 0

def _make_item_and_artefact(db):
    """Create a minimal Item + Artefact for testing (unique names each call)."""
    global _counter
    _counter += 1
    from arcology_shared.enums import ArtefactType
    from myapp.database import Artefact, Item, Platform

    platform = Platform(name=f'Test Platform {_counter}')
    db.session.add(platform)
    db.session.flush()

    item = Item(name=f'Test Item {_counter}', platform_id=platform.id)
    db.session.add(item)
    db.session.flush()

    artefact = Artefact(
        item_id=item.id,
        label=f'Test Artefact {_counter}',
        artefact_type=ArtefactType.RAW_SECTOR,
        original_filename='test.img',
        storage_path=f'test_{_counter}.img',
    )
    db.session.add(artefact)
    db.session.flush()
    return item, artefact


# =============================================================================
# ArtefactRestriction model tests
# =============================================================================

class TestArtefactRestriction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_add_restriction(self):
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)

            r = ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.MALWARE,
                reason='Contains known virus',
            )
            self.db.session.add(r)
            self.db.session.commit()

            self.assertTrue(artefact.is_restricted)
            self.assertEqual(len(artefact.restrictions), 1)
            self.assertEqual(artefact.restrictions[0].restriction_type, RestrictionType.MALWARE)
            self.assertEqual(artefact.restrictions[0].reason, 'Contains known virus')

    def test_multiple_restriction_types(self):
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()

            self.assertEqual(len(artefact.restrictions), 2)
            types = {r.restriction_type for r in artefact.restrictions}
            self.assertEqual(types, {RestrictionType.MALWARE, RestrictionType.PII})

    def test_unique_constraint_prevents_duplicate(self):
        with self.app.app_context():
            from sqlalchemy.exc import IntegrityError
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            with self.assertRaises(IntegrityError):
                self.db.session.commit()
            self.db.session.rollback()

    def test_cascade_delete_artefact(self):
        """Deleting an artefact should cascade-delete its restrictions."""
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            art_id = artefact.id

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.EXPLICIT,
            ))
            self.db.session.commit()

            self.assertEqual(
                ArtefactRestriction.query.filter_by(artefact_id=art_id).count(), 1
            )

            self.db.session.delete(artefact)
            self.db.session.commit()

            self.assertEqual(
                ArtefactRestriction.query.filter_by(artefact_id=art_id).count(), 0
            )


# =============================================================================
# UserRestrictionBypass tests
# =============================================================================

class TestUserRestrictionBypass(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_can_bypass_restriction(self):
        with self.app.app_context():
            from myapp.database import RestrictionType, User, UserRestrictionBypass

            user = User(username='bypass_user', password_hash='x' * 60)
            self.db.session.add(user)
            self.db.session.flush()

            self.db.session.add(UserRestrictionBypass(
                user_id=user.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.commit()

            self.assertTrue(user.can_bypass_restriction(RestrictionType.MALWARE))
            self.assertFalse(user.can_bypass_restriction(RestrictionType.PII))

    def test_can_bypass_all_restrictions(self):
        with self.app.app_context():
            from myapp.database import (
                ArtefactRestriction,
                RestrictionType,
                User,
                UserRestrictionBypass,
            )

            user = User(username='bypass_all_user', password_hash='x' * 60)
            self.db.session.add(user)
            self.db.session.flush()

            # User can bypass MALWARE and PII
            self.db.session.add(UserRestrictionBypass(
                user_id=user.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.add(UserRestrictionBypass(
                user_id=user.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()

            _, artefact = _make_item_and_artefact(self.db)
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.commit()

            # Can bypass — artefact only has MALWARE
            self.assertTrue(user.can_bypass_all_restrictions(artefact.restrictions))

            # Add PII — still can bypass
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()
            self.assertTrue(user.can_bypass_all_restrictions(artefact.restrictions))

            # Add COPYRIGHT — cannot bypass
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()
            self.assertFalse(user.can_bypass_all_restrictions(artefact.restrictions))

    def test_admin_bypasses_all_restrictions(self):
        """Admin users should implicitly bypass all restriction types."""
        with self.app.app_context():
            from myapp.database import (
                ArtefactRestriction,
                RestrictionType,
                User,
            )

            admin = User(username='admin_bypass_user', password_hash='x' * 60, is_admin=True)
            self.db.session.add(admin)
            self.db.session.flush()

            _, artefact = _make_item_and_artefact(self.db)
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.CORRUPTED,
            ))
            self.db.session.commit()

            # Admin has no explicit bypasses but can bypass everything
            self.assertEqual(len(admin.restriction_bypasses), 0)
            self.assertTrue(admin.can_bypass_restriction(RestrictionType.MALWARE))
            self.assertTrue(admin.can_bypass_restriction(RestrictionType.EXPLICIT))
            self.assertTrue(admin.can_bypass_all_restrictions(artefact.restrictions))


# =============================================================================
# API download restriction tests
# =============================================================================

class TestAPIDownloadRestriction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def test_restricted_artefact_returns_403(self):
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.MALWARE,
                reason='Test malware',
            ))
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn('restrictions', data)
        self.assertIn('malware', data['restrictions'])

    def test_unrestricted_artefact_not_blocked(self):
        """An unrestricted artefact should not return 403 (may 404 due to missing file)."""
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        # Should be 404 (file doesn't exist on disk) not 403
        self.assertNotEqual(resp.status_code, 403)


def _make_user_with_key(db, username, *, is_admin=False):
    """Create a READ_WRITE user with an API key; return (user, raw_key)."""
    from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission
    user = User(username=username, password_hash='x', is_admin=is_admin,
                permission=UserPermission.READ_WRITE, can_use_api=True)
    db.session.add(user)
    db.session.flush()
    key_obj, raw = ApiKey.create(user_id=user.id, name=f'{username}-key',
                                 permission=ApiKeyPermission.READ_WRITE)
    db.session.add(key_obj)
    db.session.commit()
    return user, raw


class TestAPIDownloadBypass(unittest.TestCase):
    """A user API key honours the owning user's restriction bypass, like the website."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def test_user_key_without_bypass_blocked(self):
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id, restriction_type=RestrictionType.COPYRIGHT))
            _, raw = _make_user_with_key(self.db, 'nobypass')
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download', headers={'X-API-Key': raw})
        self.assertEqual(resp.status_code, 403)

    def test_user_key_with_global_bypass_allowed(self):
        with self.app.app_context():
            from myapp.database import (
                ArtefactRestriction,
                RestrictionType,
                UserRestrictionBypass,
            )
            _, artefact = _make_item_and_artefact(self.db)
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id, restriction_type=RestrictionType.COPYRIGHT))
            user, raw = _make_user_with_key(self.db, 'globalbypass')
            self.db.session.add(UserRestrictionBypass(
                user_id=user.id, restriction_type=RestrictionType.COPYRIGHT))
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download', headers={'X-API-Key': raw})
        # Bypass clears the 403 gate; file is absent on disk so 404, never 403.
        self.assertNotEqual(resp.status_code, 403)

    def test_user_key_with_per_artefact_grant_allowed(self):
        with self.app.app_context():
            from myapp.database import (
                ArtefactRestriction,
                RestrictionType,
                UserArtefactBypass,
            )
            _, artefact = _make_item_and_artefact(self.db)
            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id, restriction_type=RestrictionType.COPYRIGHT))
            user, raw = _make_user_with_key(self.db, 'pagrant')
            self.db.session.flush()
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT))
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download', headers={'X-API-Key': raw})
        self.assertNotEqual(resp.status_code, 403)


class TestAPIArtefactContainsRestrictedFile(unittest.TestCase):
    """API artefact download is blocked when extracted contents are restricted.

    Parity with the website's _check_artefact_file_restrictions: downloading the
    original must be refused when a file within it carries a restriction the
    caller cannot bypass.
    """

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def _artefact_with_restricted_file(self, rtype):
        from myapp.database import ExtractedFileRestriction
        _, artefact = _make_item_and_artefact(self.db)
        _, ef = _make_partition_and_file(self.db, artefact, path='inner.bin')
        self.db.session.add(ExtractedFileRestriction(
            extracted_file_id=ef.id, restriction_type=rtype))
        self.db.session.flush()
        return artefact

    def test_worker_blocked_when_contains_restricted_file(self):
        with self.app.app_context():
            from myapp.database import RestrictionType
            artefact = self._artefact_with_restricted_file(RestrictionType.EXPLICIT)
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download', headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 403)
        self.assertIn('explicit', resp.get_json()['restrictions'])

    def test_user_with_grant_can_download_artefact_with_restricted_file(self):
        with self.app.app_context():
            from myapp.database import RestrictionType, UserArtefactBypass
            artefact = self._artefact_with_restricted_file(RestrictionType.EXPLICIT)
            user, raw = _make_user_with_key(self.db, 'filegrant')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.EXPLICIT))
            self.db.session.commit()
            uuid = artefact.uuid

        resp = self.client.get(
            f'/api/artefacts/{uuid}/download', headers={'X-API-Key': raw})
        self.assertNotEqual(resp.status_code, 403)


# =============================================================================
# artefact_to_dict serialization tests
# =============================================================================

class TestArtefactSerialization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_artefact_to_dict_includes_restrictions(self):
        with self.app.app_context():
            from myapp.blueprints.api import artefact_to_dict
            from myapp.database import ArtefactRestriction, RestrictionType

            _, artefact = _make_item_and_artefact(self.db)

            d = artefact_to_dict(artefact)
            self.assertIn('restrictions', d)
            self.assertIn('is_restricted', d)
            self.assertEqual(d['restrictions'], [])
            self.assertFalse(d['is_restricted'])

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()

            d = artefact_to_dict(artefact)
            self.assertEqual(d['restrictions'], ['pii'])
            self.assertTrue(d['is_restricted'])


# =============================================================================
# find_all_known_files_batch tests
# =============================================================================

class TestFindAllKnownFilesBatch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_returns_multiple_databases(self):
        """When a file's hash appears in two active databases, both are returned."""
        with self.app.app_context():
            from myapp.database import (
                ExtractedFile,
                FilesystemType,
                HashDatabase,
                KnownFile,
                Partition,
            )
            from myapp.services.hash_rescan import find_all_known_files_batch

            _, artefact = _make_item_and_artefact(self.db)

            # Two hash databases
            db1 = HashDatabase(name='DB1-batch')
            db2 = HashDatabase(name='DB2-batch')
            self.db.session.add_all([db1, db2])
            self.db.session.flush()

            # Same file hash in both databases
            kf1 = KnownFile(database_id=db1.id, filename='test.dat',
                            md5='aaaa1111bbbb2222cccc3333dddd4444', file_size=100)
            kf2 = KnownFile(database_id=db2.id, filename='test.dat',
                            md5='aaaa1111bbbb2222cccc3333dddd4444', file_size=100)
            self.db.session.add_all([kf1, kf2])
            self.db.session.flush()

            # Partition + ExtractedFile
            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='test.dat',
                filename='test.dat',
                md5='aaaa1111bbbb2222cccc3333dddd4444',
                file_size=100,
                is_known=True,
                known_file_id=kf1.id,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            result = find_all_known_files_batch([ef])
            self.assertIn(ef.id, result)
            matches = result[ef.id]
            db_ids = {kf.database_id for kf in matches}
            self.assertEqual(db_ids, {db1.id, db2.id})

    def test_empty_list_returns_empty(self):
        with self.app.app_context():
            from myapp.services.hash_rescan import find_all_known_files_batch
            self.assertEqual(find_all_known_files_batch([]), {})


# =============================================================================
# apply_database_restrictions tests
# =============================================================================

class TestApplyDatabaseRestrictions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_auto_restrict_from_flagged_database(self):
        """When a HashDatabase has restriction_type set and a file matches,
        apply_database_restrictions() should create the restriction."""
        with self.app.app_context():
            from myapp.database import (
                ExtractedFile,
                FilesystemType,
                HashDatabase,
                KnownFile,
                Partition,
                RestrictionType,
            )
            from myapp.services.hash_rescan import apply_database_restrictions

            _, artefact = _make_item_and_artefact(self.db)

            # Hash database flagged as MALWARE
            db_malware = HashDatabase(
                name='Malware DB',
                restriction_type=RestrictionType.MALWARE,
            )
            self.db.session.add(db_malware)
            self.db.session.flush()

            kf = KnownFile(database_id=db_malware.id, filename='virus.exe',
                           md5='deadbeef' * 4, file_size=666)
            self.db.session.add(kf)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='virus.exe',
                filename='virus.exe',
                md5='deadbeef' * 4,
                file_size=666,
                is_known=True,
                known_file_id=kf.id,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            self.assertFalse(artefact.is_restricted)

            added = apply_database_restrictions(artefact)
            self.assertEqual(added, 1)
            self.assertTrue(artefact.is_restricted)
            self.assertEqual(artefact.restrictions[0].restriction_type, RestrictionType.MALWARE)
            self.assertIn('Malware DB', artefact.restrictions[0].reason)

    def test_no_restriction_for_unflagged_database(self):
        """Databases without restriction_type should not auto-restrict."""
        with self.app.app_context():
            from myapp.database import (
                ExtractedFile,
                FilesystemType,
                HashDatabase,
                KnownFile,
                Partition,
            )
            from myapp.services.hash_rescan import apply_database_restrictions

            _, artefact = _make_item_and_artefact(self.db)

            db_normal = HashDatabase(name='Normal DB')
            self.db.session.add(db_normal)
            self.db.session.flush()

            kf = KnownFile(database_id=db_normal.id, filename='normal.dat',
                           md5='11112222333344445555666677778888', file_size=100)
            self.db.session.add(kf)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='normal.dat',
                filename='normal.dat',
                md5='11112222333344445555666677778888',
                file_size=100,
                is_known=True,
                known_file_id=kf.id,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            added = apply_database_restrictions(artefact)
            self.assertEqual(added, 0)
            self.assertFalse(artefact.is_restricted)

    def test_file_restriction_applied_on_hashdb_match(self):
        """apply_database_restrictions() should also create an ExtractedFileRestriction
        on each matched file when the database has restriction_type set."""
        with self.app.app_context():
            from myapp.database import (
                ExtractedFile,
                ExtractedFileRestriction,
                FilesystemType,
                HashDatabase,
                KnownFile,
                Partition,
                RestrictionType,
            )
            from myapp.services.hash_rescan import apply_database_restrictions

            _, artefact = _make_item_and_artefact(self.db)

            db_malware = HashDatabase(
                name='Malware DB 2',
                restriction_type=RestrictionType.MALWARE,
            )
            self.db.session.add(db_malware)
            self.db.session.flush()

            kf = KnownFile(database_id=db_malware.id, filename='trojan.exe',
                           md5='aabbccdd' * 4, file_size=1234)
            self.db.session.add(kf)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='trojan.exe',
                filename='trojan.exe',
                md5='aabbccdd' * 4,
                file_size=1234,
                is_known=True,
                known_file_id=kf.id,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            apply_database_restrictions(artefact)

            efr = ExtractedFileRestriction.query.filter_by(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.MALWARE,
            ).first()
            self.assertIsNotNone(efr)
            self.assertIn('Malware DB 2', efr.reason)

    def test_file_restriction_idempotent(self):
        """Calling apply_database_restrictions() twice should not create duplicate
        ExtractedFileRestriction rows."""
        with self.app.app_context():
            from myapp.database import (
                ExtractedFile,
                ExtractedFileRestriction,
                FilesystemType,
                HashDatabase,
                KnownFile,
                Partition,
                RestrictionType,
            )
            from myapp.services.hash_rescan import apply_database_restrictions

            _, artefact = _make_item_and_artefact(self.db)

            db_malware = HashDatabase(
                name='Malware DB 3',
                restriction_type=RestrictionType.MALWARE,
            )
            self.db.session.add(db_malware)
            self.db.session.flush()

            kf = KnownFile(database_id=db_malware.id, filename='dup.exe',
                           md5='11223344' * 4, file_size=99)
            self.db.session.add(kf)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='dup.exe',
                filename='dup.exe',
                md5='11223344' * 4,
                file_size=99,
                is_known=True,
                known_file_id=kf.id,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            apply_database_restrictions(artefact)
            apply_database_restrictions(artefact)  # second call

            count = ExtractedFileRestriction.query.filter_by(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.MALWARE,
            ).count()
            self.assertEqual(count, 1)


# =============================================================================
# ExtractedFileRestriction model tests
# =============================================================================

def _make_partition_and_file(db, artefact, path='file.txt', parent=None):
    """Create a Partition + ExtractedFile; reuses existing partition if artefact already has one."""
    from myapp.database import ExtractedFile, FilesystemType, Partition
    partition = Partition.query.filter_by(artefact_id=artefact.id).first()
    if partition is None:
        partition = Partition(
            artefact_id=artefact.id,
            filesystem=FilesystemType.UNKNOWN,
            partition_index=0,
        )
        db.session.add(partition)
        db.session.flush()

    ef = ExtractedFile(
        partition_id=partition.id,
        path=path,
        filename=path.rsplit('/', 1)[-1],
        parent_file_id=parent.id if parent else None,
    )
    db.session.add(ef)
    db.session.flush()
    return partition, ef


class TestExtractedFileRestriction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def test_add_restriction(self):
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, ef = _make_partition_and_file(self.db, artefact, 'secret.doc')

            r = ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.PII,
                reason='Contains personal data',
            )
            self.db.session.add(r)
            self.db.session.commit()

            self.assertTrue(ef.is_restricted)
            self.assertEqual(len(ef.restrictions), 1)
            self.assertEqual(ef.restrictions[0].restriction_type, RestrictionType.PII)
            self.assertEqual(ef.restrictions[0].reason, 'Contains personal data')

    def test_unique_constraint_prevents_duplicate(self):
        with self.app.app_context():
            from sqlalchemy.exc import IntegrityError
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, ef = _make_partition_and_file(self.db, artefact, 'dup.txt')

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            with self.assertRaises(IntegrityError):
                self.db.session.commit()
            self.db.session.rollback()

    def test_cascade_delete_on_extracted_file(self):
        """Deleting an ExtractedFile should cascade-delete its restrictions."""
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, ef = _make_partition_and_file(self.db, artefact, 'cascade.txt')
            ef_id = ef.id

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.commit()

            self.db.session.delete(ef)
            self.db.session.commit()

            self.assertEqual(
                ExtractedFileRestriction.query.filter_by(extracted_file_id=ef_id).count(), 0
            )

    def test_api_download_blocked_by_file_restriction(self):
        """GET /api/files/<uuid>/download returns 403 when the file has an EFR."""
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, ef = _make_partition_and_file(self.db, artefact, 'blocked.doc')

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()
            ef_uuid = ef.uuid

        resp = self.client.get(
            f'/api/files/{ef_uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn('restrictions', data)
        self.assertIn('pii', data['restrictions'])

    def test_api_download_sibling_not_blocked(self):
        """A sibling file (no EFR) should not be blocked when only its sibling is restricted."""
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, ef_restricted = _make_partition_and_file(self.db, artefact, 'secret.doc')
            _, ef_sibling = _make_partition_and_file(self.db, artefact, 'public.txt')

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=ef_restricted.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()
            sibling_uuid = ef_sibling.uuid

        resp = self.client.get(
            f'/api/files/{sibling_uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        # Should not be 403 (may be 404 since file doesn't exist on disk)
        self.assertNotEqual(resp.status_code, 403)

    def test_api_download_blocked_by_parent_archive_restriction(self):
        """Downloading a file inside a restricted archive should return 403."""
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, archive = _make_partition_and_file(self.db, artefact, 'archive.zip')
            _, inner = _make_partition_and_file(self.db, artefact, 'archive.zip/inner.doc',
                                                parent=archive)

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=archive.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.commit()
            inner_uuid = inner.uuid

        resp = self.client.get(
            f'/api/files/{inner_uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn('malware', data['restrictions'])

    def test_api_download_blocked_archive_with_restricted_child(self):
        """Downloading an archive that contains a restricted file should return 403."""
        with self.app.app_context():
            from myapp.database import ExtractedFileRestriction, RestrictionType
            _, artefact = _make_item_and_artefact(self.db)
            _, archive = _make_partition_and_file(self.db, artefact, 'container.zip')
            _, inner = _make_partition_and_file(self.db, artefact, 'container.zip/virus.exe',
                                                parent=archive)

            self.db.session.add(ExtractedFileRestriction(
                extracted_file_id=inner.id,
                restriction_type=RestrictionType.MALWARE,
            ))
            self.db.session.commit()
            archive_uuid = archive.uuid

        resp = self.client.get(
            f'/api/files/{archive_uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn('malware', data['restrictions'])


# =============================================================================
# API extracted file download restriction tests
# =============================================================================

class TestAPIExtractedFileDownloadRestriction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def test_restricted_extracted_file_returns_403(self):
        with self.app.app_context():
            from myapp.database import (
                ArtefactRestriction,
                ExtractedFile,
                FilesystemType,
                Partition,
                RestrictionType,
            )
            _, artefact = _make_item_and_artefact(self.db)

            partition = Partition(
                artefact_id=artefact.id,
                filesystem=FilesystemType.UNKNOWN,
                partition_index=0,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                path='secret.doc',
                filename='secret.doc',
            )
            self.db.session.add(ef)
            self.db.session.flush()
            ef_uuid = ef.uuid

            self.db.session.add(ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()

        resp = self.client.get(
            f'/api/files/{ef_uuid}/download',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIn('pii', data['restrictions'])


# =============================================================================
# HashDatabase restriction_type field tests
# =============================================================================

class TestHashDatabaseRestrictionField(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_restriction_type_nullable(self):
        with self.app.app_context():
            from myapp.database import HashDatabase, RestrictionType

            db1 = HashDatabase(name='Normal DB Field Test')
            self.db.session.add(db1)
            self.db.session.commit()
            self.assertIsNone(db1.restriction_type)

            db2 = HashDatabase(
                name='Malware DB Field Test',
                restriction_type=RestrictionType.MALWARE,
            )
            self.db.session.add(db2)
            self.db.session.commit()
            self.assertEqual(db2.restriction_type, RestrictionType.MALWARE)


# =============================================================================
# POST /partitions/{uuid}/files/restrict
# =============================================================================

class TestApplyFileRestrictionsAPI(unittest.TestCase):
    """Tests for POST /api/partitions/{uuid}/files/restrict."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def test_applies_new_restrictions(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, ef = _make_partition_and_file(self.db, artefact, 'img/adult.jpg')
            _make_partition_and_file(self.db, artefact, 'img/safe.jpg')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': [
                {'path': 'img/adult.jpg', 'restriction_type': 'explicit',
                 'reason': 'NSFW classifier: score=0.97'},
            ]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 1)
        self.assertEqual(data['updated'], 0)
        self.assertEqual(data['not_found'], 0)

        with self.app.app_context():
            from myapp.database import ExtractedFile, RestrictionType
            ef = ExtractedFile.query.filter_by(path='img/adult.jpg').first()
            self.assertTrue(ef.is_restricted)
            self.assertEqual(ef.restrictions[0].restriction_type, RestrictionType.EXPLICIT)
            self.assertEqual(ef.restrictions[0].reason, 'NSFW classifier: score=0.97')

    def test_idempotent_updates_reason(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'img/dup.jpg')
            p_uuid = partition.uuid
            self.db.session.commit()

        payload = {'restrictions': [
            {'path': 'img/dup.jpg', 'restriction_type': 'explicit', 'reason': 'first'},
        ]}
        self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json=payload,
            headers={'X-API-Key': _WORKER_KEY},
        )
        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': [
                {'path': 'img/dup.jpg', 'restriction_type': 'explicit', 'reason': 'updated'},
            ]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 0)
        self.assertEqual(data['updated'], 1)

        with self.app.app_context():
            from myapp.database import ExtractedFile
            ef = ExtractedFile.query.filter_by(path='img/dup.jpg').first()
            self.assertEqual(len(ef.restrictions), 1)
            self.assertEqual(ef.restrictions[0].reason, 'updated')

    def test_not_found_paths_counted(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'exists.jpg')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': [
                {'path': 'exists.jpg', 'restriction_type': 'explicit'},
                {'path': 'ghost.jpg', 'restriction_type': 'explicit'},
            ]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 1)
        self.assertEqual(data['not_found'], 1)

    def test_empty_restrictions_array_is_ok(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'empty.txt')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': []},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 0)
        self.assertEqual(data['updated'], 0)
        self.assertEqual(data['not_found'], 0)

    def test_non_worker_key_returns_403(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'auth.txt')
            p_uuid = partition.uuid
            self.db.session.commit()

        from myapp.app import create_app
        from myapp.extensions import db as _db
        app2 = create_app()
        app2.config['TESTING'] = True
        with app2.app_context():
            _db.create_all()
        client2 = app2.test_client()

        resp = client2.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': []},
            headers={'X-API-Key': 'not-the-worker-key'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_invalid_restriction_type_skipped(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'skip.jpg')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': [
                {'path': 'skip.jpg', 'restriction_type': 'not_a_real_type'},
            ]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 0)

    def test_missing_path_or_type_skipped(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'present.jpg')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': [
                {'restriction_type': 'explicit'},
                {'path': 'present.jpg'},
                {'path': 'present.jpg', 'restriction_type': 'explicit'},
            ]},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['applied'], 1)

    def test_unknown_partition_returns_404(self):
        resp = self.client.post(
            '/api/partitions/00000000000000000000000000000000/files/restrict',
            json={'restrictions': []},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 404)

    def test_restrictions_not_array_returns_400(self):
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, _ = _make_partition_and_file(self.db, artefact, 'bad.txt')
            p_uuid = partition.uuid
            self.db.session.commit()

        resp = self.client.post(
            f'/api/partitions/{p_uuid}/files/restrict',
            json={'restrictions': 'should-be-a-list'},
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 400)


# =============================================================================
# GET /partitions/{uuid}/files?show_known
# =============================================================================

class TestPartitionFilesShowKnown(unittest.TestCase):
    """Tests for the show_known query parameter on GET /api/partitions/{uuid}/files."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()

    def _setup_partition(self):
        """Create a partition with one known and one unknown file. Returns (p_uuid, known_path, unknown_path)."""
        with self.app.app_context():
            _, artefact = _make_item_and_artefact(self.db)
            partition, ef_unknown = _make_partition_and_file(self.db, artefact, 'unknown.txt')
            _, ef_known = _make_partition_and_file(self.db, artefact, 'known.txt')
            ef_known.is_known = True
            self.db.session.commit()
            return partition.uuid, 'known.txt', 'unknown.txt'

    def test_default_excludes_known_files(self):
        p_uuid, known_path, unknown_path = self._setup_partition()
        resp = self.client.get(
            f'/api/partitions/{p_uuid}/files',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        paths = {f['path'] for f in resp.get_json()['files']}
        self.assertNotIn(known_path, paths)
        self.assertIn(unknown_path, paths)

    def test_show_known_false_excludes_known_files(self):
        p_uuid, known_path, unknown_path = self._setup_partition()
        resp = self.client.get(
            f'/api/partitions/{p_uuid}/files?show_known=false',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        paths = {f['path'] for f in resp.get_json()['files']}
        self.assertNotIn(known_path, paths)
        self.assertIn(unknown_path, paths)

    def test_show_known_true_includes_all_files(self):
        p_uuid, known_path, unknown_path = self._setup_partition()
        resp = self.client.get(
            f'/api/partitions/{p_uuid}/files?show_known=true',
            headers={'X-API-Key': _WORKER_KEY},
        )
        self.assertEqual(resp.status_code, 200)
        paths = {f['path'] for f in resp.get_json()['files']}
        self.assertIn(known_path, paths)
        self.assertIn(unknown_path, paths)

    def test_total_counts_reflect_filter(self):
        p_uuid, _, _ = self._setup_partition()
        resp_filtered = self.client.get(
            f'/api/partitions/{p_uuid}/files',
            headers={'X-API-Key': _WORKER_KEY},
        )
        resp_all = self.client.get(
            f'/api/partitions/{p_uuid}/files?show_known=true',
            headers={'X-API-Key': _WORKER_KEY},
        )
        total_filtered = resp_filtered.get_json()['total']
        total_all = resp_all.get_json()['total']
        self.assertGreater(total_all, total_filtered)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
