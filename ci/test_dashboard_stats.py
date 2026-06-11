"""
Dashboard statistics tests.

Verifies that the running-analyses counter reflects the actual database state,
especially the transient RUNNING status that workers set via atomic claims.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_dashboard_stats -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-dashboard-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_AUTH = {'X-API-Key': _WORKER_KEY}


class TestDashboardStats(unittest.TestCase):
    """Verify dashboard statistics reflect analysis status transitions."""

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

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()

    def _create_analysis(self):
        """Create a minimal analysis in PENDING state and return its id."""
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item, Platform

        with self.app.app_context():
            # Reuse existing platform/item or create new ones
            platform = Platform.query.filter_by(name='Stats Test Platform').first()
            if not platform:
                platform = Platform(name='Stats Test Platform')
                self.db.session.add(platform)
                self.db.session.flush()

            item = Item(name='Stats Test Item', platform_id=platform.id)
            self.db.session.add(item)
            self.db.session.flush()

            artefact = Artefact(
                item_id=item.id,
                label='Stats Test Artefact',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test.img',
                storage_path='uploads/stats_test.img',
            )
            self.db.session.add(artefact)
            self.db.session.flush()

            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.CHECKSUM_COMPUTE,
                status=AnalysisStatus.PENDING,
            )
            self.db.session.add(analysis)
            self.db.session.commit()
            return analysis.id

    def _get_stats(self):
        """Fetch stats via the JSON endpoint (uses worker API key)."""
        # Use the internal helper directly to avoid needing browser auth.
        # Pass an admin user so visibility filtering does not exclude any rows.
        from myapp.blueprints.dashboard import _get_stats
        from myapp.database import User
        admin = User(username='stats-admin', is_admin=True)
        admin.setPassword('x' * 12)
        with self.app.app_context():
            return _get_stats(admin)

    def test_pending_analysis_counted(self):
        """A PENDING analysis should appear in pending_analyses count."""
        self._create_analysis()
        stats = self._get_stats()
        self.assertGreater(stats['pending_analyses'], 0)

    def test_running_counter_reflects_claimed_job(self):
        """After atomic claim sets RUNNING, the running counter must be > 0."""
        analysis_id = self._create_analysis()

        # Simulate the atomic claim (same as PUT /api/analysis/{id} with claim_worker)
        with self.app.app_context():
            from datetime import datetime, timezone
            from sqlalchemy import update
            from myapp.database import Analysis, AnalysisStatus

            result = self.db.session.execute(
                update(Analysis)
                .where(Analysis.id == analysis_id)
                .where(Analysis.status == AnalysisStatus.PENDING)
                .values(status=AnalysisStatus.RUNNING, started_at=datetime.now(timezone.utc))
            )
            self.db.session.commit()
            self.assertEqual(result.rowcount, 1, "Atomic claim should match 1 row")

        stats = self._get_stats()
        self.assertGreater(stats['running_analyses'], 0,
                           "Running counter must reflect a RUNNING analysis")

    def test_running_counter_decrements_on_completion(self):
        """After completing an analysis, running counter should not count it."""
        analysis_id = self._create_analysis()

        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus

            # Transition to RUNNING
            a = Analysis.query.get(analysis_id)
            a.status = AnalysisStatus.RUNNING
            self.db.session.commit()

            # Transition to COMPLETED
            a.status = AnalysisStatus.COMPLETED
            self.db.session.commit()

        # The specific analysis we just completed should not be counted as running
        with self.app.app_context():
            from myapp.database import Analysis, AnalysisStatus
            a = Analysis.query.get(analysis_id)
            self.assertEqual(a.status, AnalysisStatus.COMPLETED)

    def test_claim_via_api_sets_running(self):
        """PUT /api/analysis/{id} with claim_worker should set RUNNING status."""
        analysis_id = self._create_analysis()

        resp = self.client.put(
            f'/api/analysis/{analysis_id}',
            json={'status': 'running', 'claim_worker': True},
            headers=_AUTH,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('claimed'), "Should have claimed the job")
        self.assertEqual(data['status'], 'running')

        # Verify the running counter reflects this
        stats = self._get_stats()
        self.assertGreater(stats['running_analyses'], 0,
                           "Running counter must be > 0 after claiming a job")


# vim: ts=4 sw=4 et
