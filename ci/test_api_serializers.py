"""
Tests for myapp.utils.api_serializers.

Verifies that the artefact JSON shape includes the slug fields needed by API
clients to build canonical URLs without having to recompute slugs locally.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_api_serializers -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-api-serializer-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


from myapp.extensions import db  # noqa: E402  (Query.get -> Session.get migration)


class TestArtefactToDictSlugs(unittest.TestCase):
    """artefact_to_dict() must expose artefact.slug and artefact.item.slug."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import Artefact, Item
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            db.create_all()

            item = Item(name='Serializer Test Item')
            item.slug = 'serializer-test-item'
            db.session.add(item)
            db.session.commit()

            artefact = Artefact(
                item_id=item.id,
                label='Disc 1',
                artefact_type=ArtefactType.UNKNOWN,
                original_filename='disc1.img',
                storage_path='disc1.img',
            )
            artefact.slug = 'disc-1'
            db.session.add(artefact)
            db.session.commit()

            cls.artefact_id = artefact.id
            cls.expected_artefact_slug = 'disc-1'
            cls.expected_item_slug = 'serializer-test-item'

    def test_artefact_to_dict_includes_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertIn('slug', d)
        self.assertEqual(d['slug'], self.expected_artefact_slug)

    def test_artefact_to_dict_includes_item_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertIn('item_slug', d)
        self.assertEqual(d['item_slug'], self.expected_item_slug)

    def test_analysis_tree_node_includes_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import analysis_tree_node

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            node = analysis_tree_node(artefact)

        self.assertIn('slug', node)
        self.assertEqual(node['slug'], self.expected_artefact_slug)

    def test_artefact_to_dict_omits_storage_by_default(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertNotIn('storage_path', d)
        self.assertNotIn('storage_directory', d)

    def test_artefact_to_dict_include_storage(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            d = artefact_to_dict(artefact, include_storage=True)

        self.assertEqual(d['storage_path'], 'disc1.img')
        self.assertIn('storage_directory', d)


class TestAnalysisToDictArtefactShape(unittest.TestCase):
    """analysis_to_dict(include_artefact=True) must preserve every key the
    worker reads from the embedded artefact dict (see arcworker/analysis.py
    and arcworker/analyses/*.py)."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.app import create_app
        from myapp.database import Analysis, Artefact, Item
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            db.create_all()

            item = Item(name='Worker Shape Item')
            item.slug = 'worker-shape-item'
            db.session.add(item)
            db.session.commit()

            artefact = Artefact(
                item_id=item.id,
                label='Side A',
                artefact_type=ArtefactType.SCP,
                original_filename='side-a.scp',
                storage_path='side-a.scp',
            )
            artefact.slug = 'side-a'
            db.session.add(artefact)
            db.session.commit()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
            )
            db.session.add(analysis)
            db.session.commit()

            cls.analysis_id = analysis.id

    def test_embedded_artefact_has_worker_keys_with_storage(self):
        """Worker /pending poll: include_artefact_storage=True must yield the
        keys the worker reads from the embedded artefact dict."""
        from myapp.database import Analysis
        from myapp.utils.api_serializers import analysis_to_dict

        with self.app.app_context():
            analysis = db.session.get(Analysis, self.analysis_id)
            d = analysis_to_dict(
                analysis, include_artefact=True, include_artefact_storage=True
            )

        art = d['artefact']
        # Keys read by worker/arcworker/analysis.py:105-106
        self.assertEqual(art['storage_path'], 'side-a.scp')
        self.assertIn('storage_directory', art)
        # Keys read by worker/arcworker/analyses/*.py via artefact['item'][...]
        self.assertIn('item', art)
        self.assertIn('uuid', art['item'])
        self.assertIn('slug', art['item'])
        self.assertEqual(art['item']['slug'], 'worker-shape-item')
        # Keys read elsewhere from the embedded shape
        for key in ('uuid', 'slug', 'label', 'original_filename', 'artefact_type'):
            self.assertIn(key, art)

    def test_embedded_artefact_omits_storage_by_default(self):
        """Read-only callers (/api/.../analyses, /api/analysis/failures) get
        the embedded artefact without storage_path / storage_directory."""
        from myapp.database import Analysis
        from myapp.utils.api_serializers import analysis_to_dict

        with self.app.app_context():
            analysis = db.session.get(Analysis, self.analysis_id)
            d = analysis_to_dict(analysis, include_artefact=True)

        art = d['artefact']
        self.assertNotIn('storage_path', art)
        self.assertNotIn('storage_directory', art)
        # Item shape and identity keys still present.
        self.assertEqual(art['item']['slug'], 'worker-shape-item')
        self.assertIn('slug', art)


class TestOrphanEnumSerialization(unittest.TestCase):
    """A row whose enum column holds a value absent from the Python enum
    (left behind when a feature-branch migration is downgraded without
    cleaning up its rows) is read back as None by the _TolerantEnum
    crash-shield.  The serializers must tolerate that None rather than
    raising AttributeError on `.value` — otherwise /api/analysis/pending
    returns 500 on every poll and the worker can never drain the queue.
    """

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.app import create_app
        from myapp.database import Analysis, Artefact, Item
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            db.create_all()

            item = Item(name='Orphan Enum Item')
            item.slug = 'orphan-enum-item'
            db.session.add(item)
            db.session.commit()

            artefact = Artefact(
                item_id=item.id,
                label='Orphan Side',
                artefact_type=ArtefactType.SCP,
                original_filename='orphan.scp',
                storage_path='orphan.scp',
            )
            artefact.slug = 'orphan-side'
            db.session.add(artefact)
            db.session.commit()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
            )
            db.session.add(analysis)
            db.session.commit()

            # Simulate an orphan row: write enum values that no longer exist
            # in the Python enums directly into the columns, bypassing the ORM.
            db.session.execute(
                db.text('UPDATE analyses SET analysis_type = :v WHERE id = :i'),
                {'v': 'NSFW_SCAN', 'i': analysis.id},
            )
            db.session.execute(
                db.text('UPDATE artefacts SET artefact_type = :v WHERE id = :i'),
                {'v': 'GONE_TYPE', 'i': artefact.id},
            )
            db.session.commit()
            db.session.expire_all()

            cls.analysis_id = analysis.id
            cls.artefact_id = artefact.id

    def test_tolerant_enum_reads_back_as_none(self):
        from myapp.database import Analysis, Artefact

        with self.app.app_context():
            analysis = db.session.get(Analysis, self.analysis_id)
            artefact = db.session.get(Artefact, self.artefact_id)
            self.assertIsNone(analysis.analysis_type)
            self.assertIsNone(artefact.artefact_type)

    def test_analysis_to_dict_survives_orphan_type(self):
        import json
        from myapp.database import Analysis
        from myapp.utils.api_serializers import analysis_to_dict

        with self.app.app_context():
            analysis = db.session.get(Analysis, self.analysis_id)
            d = analysis_to_dict(
                analysis, include_artefact=True, include_artefact_storage=True
            )

        self.assertIsNone(d['analysis_type'])
        self.assertIsNone(d['artefact']['artefact_type'])
        # Must still be JSON-serialisable (this is what jsonify() does).
        json.dumps(d)

    def test_artefact_to_dict_survives_orphan_type(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = db.session.get(Artefact, self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertIsNone(d['artefact_type'])


class TestFileRestrictionMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission

        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            db.create_all()
            user = User(username='metadata-reader', password_hash='unused',
                        permission=UserPermission.READ_ONLY, can_use_api=True)
            db.session.add(user)
            db.session.flush()
            key, raw = ApiKey.create(user_id=user.id, name='metadata-read',
                                     permission=ApiKeyPermission.READ_ONLY)
            db.session.add(key)
            db.session.commit()
            cls.auth = {'X-API-Key': raw}

    def _make_partition(self, n):
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ArtefactRestriction,
            ExtractedFile,
            ExtractedFileRestriction,
            FilesystemType,
            Item,
            Partition,
            RestrictionType,
        )
        with self.app.app_context():
            item = Item(name=f'metadata-{n}')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='infected container',
                           artefact_type=ArtefactType.ZIP, storage_path='sample.zip',
                           original_filename='sample.zip')
            db.session.add(art)
            db.session.flush()
            db.session.add(ArtefactRestriction(artefact_id=art.id,
                                               restriction_type=RestrictionType.MALWARE))
            part = Partition(artefact_id=art.id, filesystem=FilesystemType.UNKNOWN)
            db.session.add(part)
            db.session.flush()
            for i in range(n):
                f = ExtractedFile(partition_id=part.id, path=f'!Foo/file{i:03}',
                                  filename=f'file{i:03}')
                db.session.add(f)
                db.session.flush()
                if i != 0:
                    db.session.add(ExtractedFileRestriction(
                        extracted_file_id=f.id, restriction_type=RestrictionType.MALWARE))
                    db.session.add(ExtractedFileRestriction(
                        extracted_file_id=f.id, restriction_type=RestrictionType.PII))
            db.session.commit()
            return part.uuid

    def test_listing_serializes_direct_restrictions_and_preserves_pagination(self):
        uuid = self._make_partition(4)
        r = self.client.get(f'/api/partitions/{uuid}/files?show_known=true&per_page=2',
                            headers=self.auth)
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertEqual((data['total'], data['pages'], len(data['files'])), (4, 2, 2))
        self.assertEqual(data['files'][0]['restrictions'], [])  # No container-wide taint.
        self.assertEqual(set(data['files'][1]['restrictions']), {'malware', 'pii'})
        r = self.client.get(f'/api/partitions/{uuid}/files?show_known=true&per_page=2&page=2',
                            headers=self.auth)
        self.assertEqual(len(r.get_json()['files']), 2)

    def test_file_serializer_exposes_restriction_values(self):
        from myapp.database import ExtractedFile, Partition
        from myapp.utils.api_serializers import file_to_dict

        uuid = self._make_partition(2)
        with self.app.app_context():
            files = db.session.scalars(db.select(ExtractedFile).join(Partition)
                                       .where(Partition.uuid == uuid)
                                       .order_by(ExtractedFile.path)).all()
            self.assertEqual(file_to_dict(files[0])['restrictions'], [])
            self.assertEqual(set(file_to_dict(files[1])['restrictions']), {'malware', 'pii'})

    def test_listing_query_count_does_not_grow_per_file(self):
        from sqlalchemy import event

        counts = []
        for n in (2, 30):
            uuid = self._make_partition(n)
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany, recorded=statements):
                if statement.lstrip().upper().startswith('SELECT'):
                    recorded.append(statement)
            with self.app.app_context():
                engine = db.engine
            event.listen(engine, 'before_cursor_execute', record)
            try:
                r = self.client.get(f'/api/partitions/{uuid}/files?show_known=true&per_page=100',
                                    headers=self.auth)
                self.assertEqual(r.status_code, 200, r.data)
                self.assertEqual(len(r.get_json()['files']), n)
            finally:
                event.remove(engine, 'before_cursor_execute', record)
            counts.append(len(statements))
        self.assertEqual(counts[0], counts[1], counts)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
