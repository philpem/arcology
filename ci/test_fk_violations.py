"""
Foreign key violation tests.

Verifies that deleting records does not cause IntegrityError / 500 errors due
to dangling foreign key references.  Covers:

  - Cascade deletes: Item -> Artefacts, Artefact -> Analyses/Partitions/derived,
    Partition -> ExtractedFiles/RecognisedProducts, HashDatabase -> KnownFiles/Products
  - Defensive checks: Platform/Category/ExternalSystem refuse deletion when
    referenced by children or items
  - M2M cleanup: deleting a Tag with associations works cleanly
  - Edge cases: deleting records that are referenced by nullable FKs
    (KnownFile -> ExtractedFile, Analysis -> derived Artefact, Platform -> HashDatabase)

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_fk_violations -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-fk-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _enable_sqlite_fks(app, _db):
    """Enable SQLite foreign key enforcement so tests match PostgreSQL behaviour."""
    from sqlalchemy import event

    @event.listens_for(_db.engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _create_app_and_db():
    """Create a fresh app and database for each test class."""
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _enable_sqlite_fks(app, _db)
        _db.create_all()
    return app, _db


# =============================================================================
# Cascade delete tests — verify ORM cascades prevent FK violations
# =============================================================================

class TestItemCascadeDelete(unittest.TestCase):
    """Deleting an Item must cascade to Artefacts, ExternalReferences, and tags."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_item_cascades_to_artefacts(self):
        """Deleting an Item should cascade-delete all its Artefacts."""
        with self.app.app_context():
            from myapp.database import Artefact, Item, Platform
            from shared.enums import ArtefactType

            platform = Platform(name='Cascade Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Cascade Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()
            item_id = item.id

            for i in range(3):
                self.db.session.add(Artefact(
                    item_id=item.id,
                    label=f'Art {i}',
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename=f'art{i}.img',
                    storage_path=f'uploads/art{i}.img',
                ))
            self.db.session.commit()

            self.assertEqual(Artefact.query.filter_by(item_id=item_id).count(), 3)

            self.db.session.delete(item)
            self.db.session.commit()

            self.assertEqual(Artefact.query.filter_by(item_id=item_id).count(), 0)

    def test_delete_item_cascades_to_external_references(self):
        """Deleting an Item should cascade-delete its ExternalReferences."""
        with self.app.app_context():
            from myapp.database import ExternalReference, ExternalSystem, Item, Platform

            platform = Platform(name='Ref Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Ref Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()
            item_id = item.id

            system = ExternalSystem(name='Test System', base_url='https://example.com')
            self.db.session.add(system)
            self.db.session.flush()

            ref = ExternalReference(item_id=item.id, system_id=system.id,
                                    external_id='EXT-001')
            self.db.session.add(ref)
            self.db.session.commit()

            self.assertEqual(ExternalReference.query.filter_by(item_id=item_id).count(), 1)

            self.db.session.delete(item)
            self.db.session.commit()

            self.assertEqual(ExternalReference.query.filter_by(item_id=item_id).count(), 0)
            # The ExternalSystem should survive
            self.assertIsNotNone(ExternalSystem.query.filter_by(name='Test System').first())

    def test_delete_item_cleans_up_tag_associations(self):
        """Deleting an Item should remove M2M tag associations but not the Tags."""
        with self.app.app_context():
            from myapp.database import Item, Platform, Tag

            platform = Platform(name='Tag Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Tagged Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            tag = Tag(name='test-tag-item-cascade')
            self.db.session.add(tag)
            self.db.session.flush()
            tag_id = tag.id

            item.tags.append(tag)
            self.db.session.commit()

            self.db.session.delete(item)
            self.db.session.commit()

            # Tag should survive
            self.assertIsNotNone(Tag.query.get(tag_id))


class TestArtefactCascadeDelete(unittest.TestCase):
    """Deleting an Artefact must cascade to Analyses, Partitions, derived Artefacts, etc."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    _item_counter = 0

    def _make_item(self):
        from myapp.database import Item, Platform
        TestArtefactCascadeDelete._item_counter += 1
        n = TestArtefactCascadeDelete._item_counter
        platform = Platform(name=f'Art Cascade Platform {n}')
        self.db.session.add(platform)
        self.db.session.flush()
        item = Item(name=f'Art Cascade Item {n}', platform_id=platform.id)
        self.db.session.add(item)
        self.db.session.flush()
        return item

    def test_delete_artefact_cascades_to_analyses(self):
        """Deleting an Artefact should cascade-delete all its Analysis records."""
        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus, Artefact
            from shared.enums import AnalysisType, ArtefactType

            item = self._make_item()
            artefact = Artefact(
                item_id=item.id, label='With Analysis',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test.img', storage_path='uploads/test.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_id = artefact.id

            analysis = Analysis(
                artefact_id=art_id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.commit()

            self.assertEqual(Analysis.query.filter_by(artefact_id=art_id).count(), 1)

            self.db.session.delete(artefact)
            self.db.session.commit()

            self.assertEqual(Analysis.query.filter_by(artefact_id=art_id).count(), 0)

    def test_delete_artefact_cascades_to_partitions_and_files(self):
        """Deleting an Artefact should cascade-delete Partitions and their ExtractedFiles."""
        with self.app.app_context():
            from myapp.database import Artefact, ExtractedFile, FilesystemType, Partition
            from shared.enums import ArtefactType

            item = self._make_item()
            artefact = Artefact(
                item_id=item.id, label='With Partition',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='disk.img', storage_path='uploads/disk.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_id = artefact.id

            partition = Partition(
                artefact_id=art_id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()
            part_id = partition.id

            ef = ExtractedFile(
                partition_id=part_id,
                filename='AUTOEXEC.BAT',
                path='/AUTOEXEC.BAT',
                file_size=128,
            )
            self.db.session.add(ef)
            self.db.session.commit()

            self.assertEqual(Partition.query.filter_by(artefact_id=art_id).count(), 1)
            self.assertEqual(ExtractedFile.query.filter_by(partition_id=part_id).count(), 1)

            self.db.session.delete(artefact)
            self.db.session.commit()

            self.assertEqual(Partition.query.filter_by(artefact_id=art_id).count(), 0)
            self.assertEqual(ExtractedFile.query.filter_by(partition_id=part_id).count(), 0)

    def test_delete_artefact_cascades_to_derived_artefacts(self):
        """Deleting a parent Artefact should cascade-delete derived Artefacts."""
        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus, Artefact
            from shared.enums import AnalysisType, ArtefactType

            item = self._make_item()
            parent = Artefact(
                item_id=item.id, label='Parent',
                artefact_type=ArtefactType.SCP,
                original_filename='flux.scp', storage_path='uploads/flux.scp',
            )
            self.db.session.add(parent)
            self.db.session.flush()
            parent_id = parent.id

            analysis = Analysis(
                artefact_id=parent_id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.flush()

            derived = Artefact(
                item_id=item.id, label='Derived',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='decoded.img', storage_path='outputs/decoded.img',
                parent_artefact_id=parent_id,
                derived_from_analysis_id=analysis.id,
            )
            self.db.session.add(derived)
            self.db.session.commit()

            self.db.session.delete(parent)
            self.db.session.commit()

            # Both the derived artefact and analysis should be gone
            self.assertEqual(Artefact.query.filter_by(id=parent_id).count(), 0)
            self.assertIsNone(Artefact.query.filter_by(label='Derived',
                                                       parent_artefact_id=parent_id).first())

    def test_delete_artefact_cascades_to_protection_indicators(self):
        """Deleting an Artefact should cascade-delete ArtefactProtection rows."""
        with self.app.app_context():
            from myapp.database import Artefact, ArtefactProtection
            from shared.enums import ArtefactType

            item = self._make_item()
            artefact = Artefact(
                item_id=item.id, label='Protected',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='prot.img', storage_path='uploads/prot.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_id = artefact.id

            prot = ArtefactProtection(artefact_id=art_id, protection_type='copy_protection')
            self.db.session.add(prot)
            self.db.session.commit()

            self.db.session.delete(artefact)
            self.db.session.commit()

            self.assertEqual(ArtefactProtection.query.filter_by(artefact_id=art_id).count(), 0)

    def test_delete_artefact_cascades_to_mastering_indicators(self):
        """Deleting an Artefact should cascade-delete ArtefactMastering rows."""
        with self.app.app_context():
            from myapp.database import Artefact, ArtefactMastering
            from shared.enums import ArtefactType

            item = self._make_item()
            artefact = Artefact(
                item_id=item.id, label='Mastered',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='mast.img', storage_path='uploads/mast.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_id = artefact.id

            mast = ArtefactMastering(artefact_id=art_id, mastering_type='master_disc')
            self.db.session.add(mast)
            self.db.session.commit()

            self.db.session.delete(artefact)
            self.db.session.commit()

            self.assertEqual(ArtefactMastering.query.filter_by(artefact_id=art_id).count(), 0)

    def test_delete_artefact_cleans_up_tag_associations(self):
        """Deleting an Artefact should remove tag M2M associations but not the Tags."""
        with self.app.app_context():
            from myapp.database import Artefact, Tag
            from shared.enums import ArtefactType

            item = self._make_item()
            artefact = Artefact(
                item_id=item.id, label='Tagged Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='tagged.img', storage_path='uploads/tagged.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            tag = Tag(name='test-tag-artefact-cascade')
            self.db.session.add(tag)
            self.db.session.flush()
            tag_id = tag.id

            artefact.tags.append(tag)
            self.db.session.commit()

            self.db.session.delete(artefact)
            self.db.session.commit()

            # Tag should survive
            self.assertIsNotNone(Tag.query.get(tag_id))


class TestDeepCascadeDelete(unittest.TestCase):
    """Test cascades through multiple levels: Item -> Artefact -> Analysis/Partition -> ...."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_item_cascades_through_full_hierarchy(self):
        """Deleting an Item should cascade through Artefact -> Partition -> ExtractedFile."""
        with self.app.app_context():
            from myapp.database import (
                Analysis,
                AnalysisStatus,
                Artefact,
                ArtefactMastering,
                ArtefactProtection,
                ExtractedFile,
                FilesystemType,
                Item,
                Partition,
                Platform,
            )
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='Deep Cascade Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Deep Cascade Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='Deep Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='deep.img', storage_path='uploads/deep.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.flush()

            # Derived artefact with its own partition and files
            derived = Artefact(
                item_id=item.id, label='Deep Derived',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='derived.img', storage_path='outputs/derived.img',
                parent_artefact_id=artefact.id,
                derived_from_analysis_id=analysis.id,
            )
            self.db.session.add(derived)
            self.db.session.flush()

            partition = Partition(
                artefact_id=derived.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                filename='README.TXT',
                path='/README.TXT',
                file_size=64,
            )
            self.db.session.add(ef)

            prot = ArtefactProtection(artefact_id=artefact.id, protection_type='weak_bits')
            mast = ArtefactMastering(artefact_id=artefact.id, mastering_type='master_disc')
            self.db.session.add_all([prot, mast])
            self.db.session.commit()

            # Collect IDs for post-delete verification
            art_id = artefact.id
            derived_id = derived.id
            analysis_id = analysis.id
            part_id = partition.id

            # This single delete should cascade through everything
            self.db.session.delete(item)
            self.db.session.commit()

            self.assertEqual(Artefact.query.filter_by(id=art_id).count(), 0)
            self.assertEqual(Artefact.query.filter_by(id=derived_id).count(), 0)
            self.assertEqual(Analysis.query.filter_by(id=analysis_id).count(), 0)
            self.assertEqual(Partition.query.filter_by(id=part_id).count(), 0)
            self.assertEqual(ArtefactProtection.query.filter_by(artefact_id=art_id).count(), 0)
            self.assertEqual(ArtefactMastering.query.filter_by(artefact_id=art_id).count(), 0)

            # Platform should survive
            self.assertIsNotNone(Platform.query.filter_by(name='Deep Cascade Platform').first())


# =============================================================================
# Taxonomy defensive checks — refuse deletion when FK dependants exist
# =============================================================================

class TestTaxonomyDeleteDefensiveChecks(unittest.TestCase):
    """Taxonomy delete routes must refuse when FK dependants exist."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = _db

        with cls.app.app_context():
            _enable_sqlite_fks(cls.app, _db)
            _db.create_all()
            # Create an admin user for login
            import bcrypt

            from myapp.database import User
            pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
            user = User(username='fkadmin', password_hash=pw, is_admin=True)
            _db.session.add(user)
            _db.session.commit()

    def _login(self):
        self.client.post('/login', data={
            'username': 'fkadmin',
            'password': 'testpassword1234',
        }, follow_redirects=True)

    def test_delete_platform_with_items_rejected(self):
        """Deleting a Platform with associated Items should be rejected (not 500)."""
        with self.app.app_context():
            from myapp.database import Item, Platform

            platform = Platform(name='Platform With Items')
            self.db.session.add(platform)
            self.db.session.flush()
            plat_id = platform.id

            item = Item(name='FK Item', platform_id=plat_id)
            self.db.session.add(item)
            self.db.session.commit()

        self._login()
        resp = self.client.post(f'/taxonomy/platforms/{plat_id}/delete',
                                follow_redirects=True)
        self.assertNotEqual(resp.status_code, 500)

        with self.app.app_context():
            from myapp.database import Platform
            self.assertIsNotNone(Platform.query.get(plat_id),
                                'Platform should not have been deleted')

    def test_delete_platform_with_children_rejected(self):
        """Deleting a Platform with child Platforms should be rejected (not 500)."""
        with self.app.app_context():
            from myapp.database import Platform

            parent = Platform(name='Parent Platform')
            self.db.session.add(parent)
            self.db.session.flush()
            parent_id = parent.id

            child = Platform(name='Child Platform', parent_id=parent_id)
            self.db.session.add(child)
            self.db.session.commit()

        self._login()
        resp = self.client.post(f'/taxonomy/platforms/{parent_id}/delete',
                                follow_redirects=True)
        self.assertNotEqual(resp.status_code, 500)

        with self.app.app_context():
            from myapp.database import Platform
            self.assertIsNotNone(Platform.query.get(parent_id),
                                'Parent platform should not have been deleted')

    def test_delete_category_with_items_rejected(self):
        """Deleting a Category with associated Items should be rejected (not 500)."""
        with self.app.app_context():
            from myapp.database import Category, Item, Platform

            platform = Platform(name='Cat Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            category = Category(name='Category With Items')
            self.db.session.add(category)
            self.db.session.flush()
            cat_id = category.id

            item = Item(name='Cat FK Item', platform_id=platform.id,
                        category_id=cat_id)
            self.db.session.add(item)
            self.db.session.commit()

        self._login()
        resp = self.client.post(f'/taxonomy/categories/{cat_id}/delete',
                                follow_redirects=True)
        self.assertNotEqual(resp.status_code, 500)

        with self.app.app_context():
            from myapp.database import Category
            self.assertIsNotNone(Category.query.get(cat_id),
                                'Category should not have been deleted')

    def test_delete_category_with_children_rejected(self):
        """Deleting a Category with child Categories should be rejected (not 500)."""
        with self.app.app_context():
            from myapp.database import Category

            parent = Category(name='Parent Category')
            self.db.session.add(parent)
            self.db.session.flush()
            parent_id = parent.id

            child = Category(name='Child Category', parent_id=parent_id)
            self.db.session.add(child)
            self.db.session.commit()

        self._login()
        resp = self.client.post(f'/taxonomy/categories/{parent_id}/delete',
                                follow_redirects=True)
        self.assertNotEqual(resp.status_code, 500)

        with self.app.app_context():
            from myapp.database import Category
            self.assertIsNotNone(Category.query.get(parent_id),
                                'Parent category should not have been deleted')

    def test_delete_external_system_with_references_rejected(self):
        """Deleting an ExternalSystem with references should be rejected (not 500)."""
        with self.app.app_context():
            from myapp.database import ExternalReference, ExternalSystem, Item, Platform

            platform = Platform(name='ExtSys Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='ExtSys Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            system = ExternalSystem(name='System With Refs',
                                    base_url='https://example.com')
            self.db.session.add(system)
            self.db.session.flush()
            sys_id = system.id

            ref = ExternalReference(item_id=item.id, system_id=sys_id,
                                    external_id='REF-001')
            self.db.session.add(ref)
            self.db.session.commit()

        self._login()
        resp = self.client.post(f'/taxonomy/external-systems/{sys_id}/delete',
                                follow_redirects=True)
        self.assertNotEqual(resp.status_code, 500)

        with self.app.app_context():
            from myapp.database import ExternalSystem
            self.assertIsNotNone(ExternalSystem.query.get(sys_id),
                                'External system should not have been deleted')


# =============================================================================
# Tag deletion — M2M associations must be cleaned up without errors
# =============================================================================

class TestTagDeletion(unittest.TestCase):
    """Deleting a Tag with M2M associations should work without FK violations."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_tag_with_item_associations(self):
        """Deleting a Tag associated with Items should not raise IntegrityError."""
        with self.app.app_context():
            from myapp.database import Item, Platform, Tag

            platform = Platform(name='Tag Del Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Tag Del Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            tag = Tag(name='doomed-item-tag')
            self.db.session.add(tag)
            self.db.session.flush()

            item.tags.append(tag)
            self.db.session.commit()

            self.assertIn(tag, item.tags)

            self.db.session.delete(tag)
            self.db.session.commit()

            # Item should survive, tag should be gone
            self.assertIsNotNone(Item.query.filter_by(name='Tag Del Item').first())
            self.assertIsNone(Tag.query.filter_by(name='doomed-item-tag').first())

    def test_delete_tag_with_artefact_associations(self):
        """Deleting a Tag associated with Artefacts should not raise IntegrityError."""
        with self.app.app_context():
            from myapp.database import Artefact, Item, Platform, Tag
            from shared.enums import ArtefactType

            platform = Platform(name='ArtTag Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='ArtTag Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='ArtTag Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='arttag.img', storage_path='uploads/arttag.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            tag = Tag(name='doomed-artefact-tag')
            self.db.session.add(tag)
            self.db.session.flush()

            artefact.tags.append(tag)
            self.db.session.commit()

            self.db.session.delete(tag)
            self.db.session.commit()

            self.assertIsNotNone(Artefact.query.filter_by(label='ArtTag Art').first())
            self.assertIsNone(Tag.query.filter_by(name='doomed-artefact-tag').first())

    def test_delete_tag_with_both_item_and_artefact_associations(self):
        """Deleting a Tag associated with both Items and Artefacts should work."""
        with self.app.app_context():
            from myapp.database import Artefact, Item, Platform, Tag
            from shared.enums import ArtefactType

            platform = Platform(name='Both Tag Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Both Tag Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='Both Tag Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='both.img', storage_path='uploads/both.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            tag = Tag(name='doomed-both-tag')
            self.db.session.add(tag)
            self.db.session.flush()

            item.tags.append(tag)
            artefact.tags.append(tag)
            self.db.session.commit()

            self.db.session.delete(tag)
            self.db.session.commit()

            self.assertIsNone(Tag.query.filter_by(name='doomed-both-tag').first())


# =============================================================================
# HashDatabase / KnownFile / KnownProduct cascades
# =============================================================================

class TestHashDatabaseCascadeDelete(unittest.TestCase):
    """Deleting a HashDatabase must cascade to KnownProducts and KnownFiles."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_database_cascades_to_known_files_and_products(self):
        """Deleting a HashDatabase should cascade-delete KnownProducts and KnownFiles."""
        with self.app.app_context():
            from myapp.database import HashDatabase, KnownFile, KnownProduct

            hdb = HashDatabase(name='Test DB', version='1.0')
            self.db.session.add(hdb)
            self.db.session.flush()
            hdb_id = hdb.id

            product = KnownProduct(database_id=hdb_id, title='Test Product')
            self.db.session.add(product)
            self.db.session.flush()
            prod_id = product.id

            kf = KnownFile(
                database_id=hdb_id,
                product_id=prod_id,
                filename='TEST.COM',
                file_size=256,
                md5='d41d8cd98f00b204e9800998ecf8427e',
            )
            self.db.session.add(kf)
            self.db.session.commit()

            self.db.session.delete(hdb)
            self.db.session.commit()

            self.assertEqual(KnownProduct.query.filter_by(database_id=hdb_id).count(), 0)
            self.assertEqual(KnownFile.query.filter_by(database_id=hdb_id).count(), 0)

    def test_delete_known_product_cascades_to_recognised_products(self):
        """Deleting a KnownProduct should cascade-delete RecognisedProduct rows."""
        with self.app.app_context():
            from myapp.database import (
                Artefact,
                FilesystemType,
                HashDatabase,
                Item,
                KnownProduct,
                Partition,
                Platform,
                RecognisedProduct,
            )
            from shared.enums import ArtefactType

            # Create hash database side
            hdb = HashDatabase(name='Recog DB', version='1.0')
            self.db.session.add(hdb)
            self.db.session.flush()

            product = KnownProduct(database_id=hdb.id, title='Recog Product')
            self.db.session.add(product)
            self.db.session.flush()
            prod_id = product.id

            # Create item/artefact/partition side
            platform = Platform(name='Recog Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Recog Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='Recog Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='recog.img', storage_path='uploads/recog.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            rp = RecognisedProduct(partition_id=partition.id, product_id=prod_id,
                                   folder_path='/apps/product')
            self.db.session.add(rp)
            self.db.session.commit()

            self.assertEqual(
                RecognisedProduct.query.filter_by(product_id=prod_id).count(), 1)

            self.db.session.delete(product)
            self.db.session.commit()

            self.assertEqual(
                RecognisedProduct.query.filter_by(product_id=prod_id).count(), 0)
            # Partition should survive
            self.assertIsNotNone(Partition.query.filter_by(id=partition.id).first())


# =============================================================================
# Nullable FK edge cases — deletion should not leave dangling references
# =============================================================================

class TestNullableFKEdgeCases(unittest.TestCase):
    """Test deletion of records referenced by nullable FKs."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_known_file_referenced_by_extracted_file(self):
        """Deleting a KnownFile referenced by ExtractedFile.known_file_id must not 500.

        ExtractedFile.known_file_id is nullable, so the FK should be set to NULL
        or the delete should cascade — either way, no IntegrityError.
        """
        with self.app.app_context():
            from myapp.database import (
                Artefact,
                ExtractedFile,
                FilesystemType,
                HashDatabase,
                Item,
                KnownFile,
                KnownProduct,
                Partition,
                Platform,
            )
            from shared.enums import ArtefactType

            hdb = HashDatabase(name='NullFK DB', version='1.0')
            self.db.session.add(hdb)
            self.db.session.flush()

            product = KnownProduct(database_id=hdb.id, title='NullFK Product')
            self.db.session.add(product)
            self.db.session.flush()

            kf = KnownFile(
                database_id=hdb.id,
                product_id=product.id,
                filename='KNOWN.COM',
                file_size=512,
                md5='abc123abc123abc123abc123abc123ab',
            )
            self.db.session.add(kf)
            self.db.session.flush()
            kf_id = kf.id

            platform = Platform(name='NullFK Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='NullFK Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='NullFK Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='nullfk.img', storage_path='uploads/nullfk.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                filename='KNOWN.COM',
                path='/KNOWN.COM',
                file_size=512,
                known_file_id=kf_id,
            )
            self.db.session.add(ef)
            self.db.session.commit()
            ef_id = ef.id

            # This is the critical test: deleting the KnownFile should not
            # cause an IntegrityError on the ExtractedFile FK.
            #
            # NOTE: SQLite does not enforce FK constraints by default, so this
            # delete will succeed even without ON DELETE SET NULL.  On PostgreSQL
            # the same operation would raise an IntegrityError if the FK lacks
            # an ON DELETE clause — which is the real scenario this test guards
            # against.  The ORM relationship on ExtractedFile.known_file has no
            # cascade or passive_deletes, so SQLAlchemy won't automatically
            # SET NULL either.  This test documents the current behaviour and
            # will catch regressions if FK enforcement is enabled.
            self.db.session.delete(kf)
            self.db.session.commit()

            # ExtractedFile should survive (the delete must not cascade to it)
            ef_after = ExtractedFile.query.get(ef_id)
            self.assertIsNotNone(ef_after,
                                 'ExtractedFile should survive KnownFile deletion')

    def test_delete_platform_referenced_by_hash_database(self):
        """Deleting a Platform referenced by HashDatabase.platform_id must not 500.

        HashDatabase.platform_id is nullable, so the FK should be set to NULL
        or cascade.  This test verifies the delete does not raise an
        IntegrityError.
        """
        with self.app.app_context():
            from myapp.database import HashDatabase, Platform

            platform = Platform(name='HashDB Platform')
            self.db.session.add(platform)
            self.db.session.flush()
            plat_id = platform.id

            hdb = HashDatabase(name='Platform Ref DB', version='1.0',
                               platform_id=plat_id)
            self.db.session.add(hdb)
            self.db.session.commit()
            hdb_id = hdb.id

            # This should not raise an IntegrityError.
            # Note: on PostgreSQL with FK enforcement, if there's no ON DELETE
            # SET NULL this would fail — flagging a real FK violation.
            self.db.session.delete(platform)
            self.db.session.commit()

            hdb_after = HashDatabase.query.get(hdb_id)
            self.assertIsNotNone(hdb_after,
                                 'HashDatabase should survive Platform deletion')

    def test_delete_extracted_file_parent_with_children(self):
        """Deleting a parent ExtractedFile with children should not cause FK violation.

        ExtractedFile.parent_file_id is a self-referential nullable FK.
        """
        with self.app.app_context():
            from myapp.database import Artefact, ExtractedFile, FilesystemType, Item, Partition, Platform
            from shared.enums import ArtefactType

            platform = Platform(name='EF Parent Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='EF Parent Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='EF Parent Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='efparent.img',
                storage_path='uploads/efparent.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            partition = Partition(
                artefact_id=artefact.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            parent_ef = ExtractedFile(
                partition_id=partition.id,
                filename='ARCHIVE.ZIP',
                path='/ARCHIVE.ZIP',
                file_size=1024,
            )
            self.db.session.add(parent_ef)
            self.db.session.flush()
            parent_ef_id = parent_ef.id

            child_ef = ExtractedFile(
                partition_id=partition.id,
                filename='README.TXT',
                path='/ARCHIVE.ZIP/README.TXT',
                file_size=64,
                parent_file_id=parent_ef_id,
            )
            self.db.session.add(child_ef)
            self.db.session.commit()
            child_ef_id = child_ef.id

            # Deleting the parent should either cascade or set NULL
            self.db.session.delete(parent_ef)
            self.db.session.commit()

            child_after = ExtractedFile.query.get(child_ef_id)
            if child_after is not None:
                # If child survived, parent_file_id should be NULL
                self.assertIsNone(child_after.parent_file_id,
                                  'parent_file_id should be NULL after parent deletion')


# =============================================================================
# API endpoint delete tests — verify no 500 on delete via REST API
# =============================================================================

class TestAPIDeleteEndpoints(unittest.TestCase):
    """REST API delete endpoints must not return 500 from FK violations."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()

    def _auth(self):
        return {'X-API-Key': os.environ['WORKER_API_KEY']}

    def test_api_delete_item_with_artefacts(self):
        """DELETE /api/items/<uuid> with artefacts should cascade (not 500)."""
        with self.app.app_context():
            from myapp.database import Artefact, Item, Platform
            from shared.enums import ArtefactType

            platform = Platform(name='API Del Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='API Del Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()
            item_uuid = item.uuid

            artefact = Artefact(
                item_id=item.id, label='API Del Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='apidel.img',
                storage_path='uploads/apidel.img',
            )
            self.db.session.add(artefact)
            self.db.session.commit()

        resp = self.client.delete(f'/api/items/{item_uuid}',
                                  headers=self._auth())
        self.assertNotEqual(resp.status_code, 500,
                            f'DELETE item returned 500: {resp.data}')

    def test_api_delete_artefact_with_analysis(self):
        """DELETE /api/artefacts/<uuid> with analyses should cascade (not 500)."""
        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus, Artefact, Item, Platform
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='API ArtDel Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='API ArtDel Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='API ArtDel Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='apiartdel.img',
                storage_path='uploads/apiartdel.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_uuid = artefact.uuid

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.commit()

        resp = self.client.delete(f'/api/artefacts/{art_uuid}',
                                  headers=self._auth())
        self.assertNotEqual(resp.status_code, 500,
                            f'DELETE artefact returned 500: {resp.data}')

    def test_api_delete_artefact_with_deep_hierarchy(self):
        """DELETE /api/artefacts/<uuid> with derived artefacts, partitions, and files."""
        with self.app.app_context():
            from myapp.database import (
                Analysis,
                AnalysisStatus,
                Artefact,
                ExtractedFile,
                FilesystemType,
                Item,
                Partition,
                Platform,
            )
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='API Deep Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='API Deep Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id, label='API Deep Art',
                artefact_type=ArtefactType.SCP,
                original_filename='deep.scp',
                storage_path='uploads/deep.scp',
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            art_uuid = artefact.uuid

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.flush()

            derived = Artefact(
                item_id=item.id, label='API Derived',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='derived.img',
                storage_path='outputs/derived.img',
                parent_artefact_id=artefact.id,
                derived_from_analysis_id=analysis.id,
            )
            self.db.session.add(derived)
            self.db.session.flush()

            partition = Partition(
                artefact_id=derived.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                filename='BOOT.SYS',
                path='/BOOT.SYS',
                file_size=2048,
            )
            self.db.session.add(ef)
            self.db.session.commit()

        resp = self.client.delete(f'/api/artefacts/{art_uuid}',
                                  headers=self._auth())
        self.assertNotEqual(resp.status_code, 500,
                            f'DELETE deep artefact returned 500: {resp.data}')


# =============================================================================
# Bulk delete tests — verify bulk_delete_item covers the full hierarchy
# =============================================================================

class TestBulkDeleteItem(unittest.TestCase):
    """bulk_delete_item() must remove all related records without FK violations."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_bulk_delete_full_hierarchy(self):
        """bulk_delete_item should remove Item + all descendants in one pass."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import bulk_delete_item
            from myapp.database import (
                Analysis,
                AnalysisStatus,
                Artefact,
                ArtefactMastering,
                ArtefactProtection,
                ArtefactRestriction,
                ExternalReference,
                ExternalSystem,
                ExtractedFile,
                ExtractedFileRestriction,
                FilesystemType,
                HashDatabase,
                Item,
                KnownProduct,
                Partition,
                Platform,
                RecognisedProduct,
                RestrictionType,
                RiscosModule,
                Tag,
            )
            from shared.enums import AnalysisType, ArtefactType

            platform = Platform(name='Bulk Del Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Bulk Del Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()
            item_id = item.id

            # Tags
            tag = Tag(name='bulk-del-tag')
            self.db.session.add(tag)
            self.db.session.flush()
            tag_id = tag.id
            item.tags.append(tag)

            # External reference
            system = ExternalSystem(name='Bulk Del System', base_url='https://example.com')
            self.db.session.add(system)
            self.db.session.flush()
            ext_ref = ExternalReference(item_id=item.id, system_id=system.id, external_id='BD-001')
            self.db.session.add(ext_ref)

            # Root artefact
            artefact = Artefact(
                item_id=item.id, label='Root Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='root.img',
                storage_path='uploads/root.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            art_tag = Tag(name='bulk-del-art-tag')
            self.db.session.add(art_tag)
            self.db.session.flush()
            art_tag_id = art_tag.id
            artefact.tags.append(art_tag)

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            self.db.session.add(analysis)
            self.db.session.flush()

            # Derived artefact
            derived = Artefact(
                item_id=item.id, label='Derived Art',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='derived.img',
                storage_path='outputs/derived.img',
                parent_artefact_id=artefact.id,
                derived_from_analysis_id=analysis.id,
            )
            self.db.session.add(derived)
            self.db.session.flush()

            # Partition + files
            partition = Partition(
                artefact_id=derived.id,
                partition_index=0,
                filesystem=FilesystemType.FAT12,
            )
            self.db.session.add(partition)
            self.db.session.flush()

            ef = ExtractedFile(
                partition_id=partition.id,
                filename='BOOT.SYS',
                path='/BOOT.SYS',
                file_size=2048,
            )
            self.db.session.add(ef)
            self.db.session.flush()

            # ExtractedFileRestriction
            efr = ExtractedFileRestriction(
                extracted_file_id=ef.id,
                restriction_type=RestrictionType.EXPLICIT,
                reason='test restriction',
            )
            self.db.session.add(efr)

            # RecognisedProduct
            hdb = HashDatabase(name='Bulk Del HDB', version='1.0')
            self.db.session.add(hdb)
            self.db.session.flush()
            kp = KnownProduct(database_id=hdb.id, title='Test Product')
            self.db.session.add(kp)
            self.db.session.flush()
            rp = RecognisedProduct(
                partition_id=partition.id,
                product_id=kp.id,
                folder_path='/',
            )
            self.db.session.add(rp)

            # Protection/mastering/module indicators
            prot = ArtefactProtection(artefact_id=artefact.id, protection_type='copylock')
            mast = ArtefactMastering(artefact_id=artefact.id, mastering_type='formaster')
            mod = RiscosModule(artefact_id=artefact.id, title_string='TestModule', version='1.0')
            rest = ArtefactRestriction(
                artefact_id=artefact.id,
                restriction_type=RestrictionType.EXPLICIT,
                reason='test',
            )
            self.db.session.add_all([prot, mast, mod, rest])
            self.db.session.commit()

            # Save IDs before bulk delete invalidates the ORM objects
            artefact_id = artefact.id
            derived_id = derived.id
            partition_id = partition.id
            ef_id = ef.id

            # Verify records exist
            self.assertEqual(Artefact.query.filter_by(item_id=item_id).count(), 2)
            self.assertGreater(ExtractedFile.query.count(), 0)

            # Run bulk delete
            bulk_delete_item(item)

            # Verify everything is gone
            self.assertIsNone(Item.query.get(item_id))
            self.assertEqual(Artefact.query.filter_by(item_id=item_id).count(), 0)
            self.assertEqual(Analysis.query.filter_by(artefact_id=artefact_id).count(), 0)
            self.assertEqual(Partition.query.filter_by(artefact_id=derived_id).count(), 0)
            self.assertEqual(ExtractedFile.query.filter_by(partition_id=partition_id).count(), 0)
            self.assertEqual(ExtractedFileRestriction.query.filter_by(extracted_file_id=ef_id).count(), 0)
            self.assertEqual(RecognisedProduct.query.filter_by(partition_id=partition_id).count(), 0)
            self.assertEqual(ArtefactProtection.query.filter_by(artefact_id=artefact_id).count(), 0)
            self.assertEqual(ArtefactMastering.query.filter_by(artefact_id=artefact_id).count(), 0)
            self.assertEqual(RiscosModule.query.filter_by(artefact_id=artefact_id).count(), 0)
            self.assertEqual(ArtefactRestriction.query.filter_by(artefact_id=artefact_id).count(), 0)
            self.assertEqual(ExternalReference.query.filter_by(item_id=item_id).count(), 0)
            # Tags and external system should survive
            self.assertIsNotNone(Tag.query.get(tag_id))
            self.assertIsNotNone(Tag.query.get(art_tag_id))
            self.assertIsNotNone(ExternalSystem.query.filter_by(name='Bulk Del System').first())

    def test_bulk_delete_empty_item(self):
        """bulk_delete_item should handle an item with no artefacts."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import bulk_delete_item
            from myapp.database import Item, Platform

            platform = Platform(name='Empty Del Platform')
            self.db.session.add(platform)
            self.db.session.flush()

            item = Item(name='Empty Del Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.commit()
            item_id = item.id

            bulk_delete_item(item)

            self.assertIsNone(Item.query.get(item_id))


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
