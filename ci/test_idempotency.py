"""
Idempotency tests for the analysis pipeline.

Verifies that:
  - POST /api/artefacts/<uuid>/analysis returns an existing PENDING/RUNNING
    analysis record instead of creating a duplicate, but allows a new record
    when the prior analysis is COMPLETED or FAILED.
  - POST /api/analysis/<id>/produce-artefact returns an existing derived
    artefact when called twice with the same storage_path, rather than
    inserting a duplicate row.
  - The database-level unique constraint on (derived_from_analysis_id,
    storage_path) prevents duplicate derived artefacts from being committed
    even if the application-level check is bypassed.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_idempotency -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-idempotency-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_AUTH = {'X-API-Key': _WORKER_KEY}


def _make_fixtures(db, app):
    """Create a minimal set of DB fixtures: Platform -> Item -> Artefact.

    Returns (item, artefact) with IDs and UUIDs populated.
    """
    from myapp.database import Platform, Item, Artefact
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
        storage_path='uploads/test.img',
    )
    db.session.add(artefact)
    db.session.commit()
    return item, artefact


# =============================================================================
# Test: POST /api/artefacts/<uuid>/analysis idempotency
# =============================================================================

class TestRequestAnalysisIdempotency(unittest.TestCase):
    """request_analysis should not create duplicate PENDING/RUNNING Analysis rows."""

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
            _, cls.artefact_uuid = (
                lambda item, art: (item, art.uuid)
            )(*_make_fixtures(_db, cls.app))

    def _post_analysis(self, analysis_type='metadata_extract'):
        return self.client.post(
            f'/api/artefacts/{self.artefact_uuid}/analysis',
            json={'analysis_type': analysis_type},
            headers=_AUTH,
        )

    def setUp(self):
        # Clean up any Analysis rows between tests
        with self.app.app_context():
            from myapp.database import Analysis
            Analysis.query.delete()
            self.db.session.commit()

    def test_second_request_returns_existing(self):
        """Second POST while PENDING returns HTTP 200 with the same analysis."""
        resp1 = self._post_analysis()
        self.assertEqual(resp1.status_code, 201)
        data1 = resp1.get_json()

        resp2 = self._post_analysis()
        self.assertEqual(resp2.status_code, 200, resp2.data)
        data2 = resp2.get_json()

        self.assertEqual(data1['id'], data2['id'])
        self.assertEqual(data1['uuid'], data2['uuid'])

    def test_second_request_does_not_create_duplicate_db_row(self):
        """Two POSTs with the same analysis_type create exactly one Analysis row."""
        self._post_analysis()
        self._post_analysis()

        with self.app.app_context():
            from myapp.database import Analysis
            from shared.enums import AnalysisType
            from myapp.database import Artefact

            artefact = Artefact.query.filter_by(uuid=self.artefact_uuid).one()
            count = Analysis.query.filter_by(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
            ).count()
            self.assertEqual(count, 1)

    def test_running_analysis_prevents_duplicate(self):
        """Second POST while analysis is RUNNING also returns HTTP 200."""
        resp1 = self._post_analysis()
        self.assertEqual(resp1.status_code, 201)
        analysis_id = resp1.get_json()['id']

        # Transition to RUNNING
        with self.app.app_context():
            from myapp.database import Analysis
            from myapp.database import AnalysisStatus
            a = Analysis.query.get(analysis_id)
            a.status = AnalysisStatus.RUNNING
            self.db.session.commit()

        resp2 = self._post_analysis()
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()['id'], analysis_id)

    def test_completed_analysis_allows_new_request(self):
        """Re-queuing after COMPLETED creates a new Analysis row (re-run is valid)."""
        resp1 = self._post_analysis()
        self.assertEqual(resp1.status_code, 201)
        first_id = resp1.get_json()['id']

        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus
            a = Analysis.query.get(first_id)
            a.status = AnalysisStatus.COMPLETED
            self.db.session.commit()

        resp2 = self._post_analysis()
        self.assertEqual(resp2.status_code, 201, resp2.data)
        self.assertNotEqual(resp2.get_json()['id'], first_id)

    def test_failed_analysis_allows_new_request(self):
        """Re-queuing after FAILED creates a new Analysis row (retry is valid)."""
        resp1 = self._post_analysis()
        self.assertEqual(resp1.status_code, 201)
        first_id = resp1.get_json()['id']

        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus
            a = Analysis.query.get(first_id)
            a.status = AnalysisStatus.FAILED
            self.db.session.commit()

        resp2 = self._post_analysis()
        self.assertEqual(resp2.status_code, 201, resp2.data)
        self.assertNotEqual(resp2.get_json()['id'], first_id)


# =============================================================================
# Test: POST /api/analysis/<id>/produce-artefact idempotency
# =============================================================================

class TestProduceArtefactIdempotency(unittest.TestCase):
    """produce_artefact should not create duplicate Artefact rows on retry."""

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
            _, cls._artefact = _make_fixtures(_db, cls.app)
            cls.artefact_id = cls._artefact.id
            cls.artefact_uuid = cls._artefact.uuid

    def setUp(self):
        # Create a fresh RUNNING analysis for each test
        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus, Artefact
            from shared.enums import AnalysisType

            # Remove artefacts derived from previous test analyses
            art = Artefact.query.get(self.artefact_id)
            for derived in list(art.derived_artefacts):
                self.db.session.delete(derived)
            Analysis.query.filter_by(artefact_id=self.artefact_id).delete()
            self.db.session.commit()

            analysis = Analysis(
                artefact_id=self.artefact_id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.RUNNING,
            )
            self.db.session.add(analysis)
            self.db.session.commit()
            self.analysis_id = analysis.id

    def _produce(self, storage_path='outputs/derived.img'):
        return self.client.post(
            f'/api/analysis/{self.analysis_id}/produce-artefact',
            json={
                'label': 'Derived Artefact',
                'original_filename': 'derived.img',
                'storage_path': storage_path,
                'artefact_type': 'raw_sector',
            },
            headers=_AUTH,
        )

    def test_second_produce_returns_existing_artefact(self):
        """Second call with same storage_path returns HTTP 200 with same uuid."""
        resp1 = self._produce()
        self.assertEqual(resp1.status_code, 201)
        uuid1 = resp1.get_json()['artefact']['uuid']

        resp2 = self._produce()
        self.assertEqual(resp2.status_code, 200, resp2.data)
        uuid2 = resp2.get_json()['artefact']['uuid']

        self.assertEqual(uuid1, uuid2)

    def test_second_produce_does_not_create_duplicate_db_row(self):
        """Two identical produce-artefact calls insert exactly one Artefact row."""
        self._produce()
        self._produce()

        with self.app.app_context():
            from myapp.database import Artefact
            count = Artefact.query.filter_by(
                derived_from_analysis_id=self.analysis_id,
            ).count()
            self.assertEqual(count, 1)

    def test_different_storage_paths_create_separate_artefacts(self):
        """Two calls with different storage_paths both succeed and create separate rows."""
        resp1 = self._produce('outputs/part0.img')
        resp2 = self._produce('outputs/part1.img')

        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 201)
        self.assertNotEqual(
            resp1.get_json()['artefact']['uuid'],
            resp2.get_json()['artefact']['uuid'],
        )

        with self.app.app_context():
            from myapp.database import Artefact
            count = Artefact.query.filter_by(
                derived_from_analysis_id=self.analysis_id,
            ).count()
            self.assertEqual(count, 2)


# =============================================================================
# Test: database-level unique constraint
# =============================================================================

class TestUniqueConstraintEnforced(unittest.TestCase):
    """The DB constraint uq_artefact_analysis_storage_path must reject duplicates."""

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
            _, cls._artefact = _make_fixtures(_db, cls.app)
            cls.artefact_id = cls._artefact.id

    def test_db_rejects_duplicate_derived_artefact(self):
        """Inserting two Artefact rows with the same (analysis_id, storage_path) raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus, Artefact
            from shared.enums import AnalysisType, ArtefactType

            analysis = Analysis(
                artefact_id=self.artefact_id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.RUNNING,
            )
            self.db.session.add(analysis)
            self.db.session.flush()

            def _make_derived(path):
                return Artefact(
                    item_id=Artefact.query.get(self.artefact_id).item_id,
                    label='Dup Test',
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename='dup.img',
                    storage_path=path,
                    parent_artefact_id=self.artefact_id,
                    derived_from_analysis_id=analysis.id,
                )

            self.db.session.add(_make_derived('outputs/dup.img'))
            self.db.session.flush()

            self.db.session.add(_make_derived('outputs/dup.img'))
            with self.assertRaises(IntegrityError):
                self.db.session.flush()

            self.db.session.rollback()

    def test_null_analysis_id_allows_multiple_same_path(self):
        """Original artefacts (analysis_id IS NULL) are exempt from the constraint."""
        with self.app.app_context():
            from myapp.database import Artefact
            from shared.enums import ArtefactType

            item_id = Artefact.query.get(self.artefact_id).item_id

            def _make_original(label):
                return Artefact(
                    item_id=item_id,
                    label=label,
                    artefact_type=ArtefactType.RAW_SECTOR,
                    original_filename='orig.img',
                    storage_path='uploads/orig.img',
                    derived_from_analysis_id=None,
                )

            self.db.session.add(_make_original('Orig A'))
            self.db.session.add(_make_original('Orig B'))
            # Should not raise — NULL is not equal to NULL in SQL
            self.db.session.flush()
            self.db.session.rollback()


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
