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
    from myapp.database import Item, Artefact, Platform
    from shared.enums import ArtefactType

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
            from myapp.database import ArtefactRestriction, RestrictionType
            from sqlalchemy.exc import IntegrityError
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
            from myapp.database import User, UserRestrictionBypass, RestrictionType

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
                User, UserRestrictionBypass, ArtefactRestriction, RestrictionType,
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
                User, ArtefactRestriction, RestrictionType,
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


# =============================================================================
# artefact_to_dict serialization tests
# =============================================================================

class TestArtefactSerialization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_artefact_to_dict_includes_restrictions(self):
        with self.app.app_context():
            from myapp.database import ArtefactRestriction, RestrictionType
            from myapp.blueprints.api import artefact_to_dict

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
                HashDatabase, KnownFile, Partition, ExtractedFile, FilesystemType,
            )
            from myapp.utils.hash_rescan import find_all_known_files_batch

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
            from myapp.utils.hash_rescan import find_all_known_files_batch
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
                HashDatabase, KnownFile, Partition, ExtractedFile,
                FilesystemType, RestrictionType,
            )
            from myapp.utils.hash_rescan import apply_database_restrictions

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

    def test_no_restriction_for_unflagged_database(self):
        """Databases without restriction_type should not auto-restrict."""
        with self.app.app_context():
            from myapp.database import (
                HashDatabase, KnownFile, Partition, ExtractedFile, FilesystemType,
            )
            from myapp.utils.hash_rescan import apply_database_restrictions

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
                ArtefactRestriction, RestrictionType, Partition,
                ExtractedFile, FilesystemType,
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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
