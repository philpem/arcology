"""
Tests for the high-level ``populate_search_index_from_analysis`` entry point
(the API path that runs on analysis completion).

Focus: the wrapper's transaction/locking behaviour around the per-type
handlers, distinct from the per-handler tests in test_replay_transcode.py.

  - A completed analysis populates its search-index rows and commits them
    atomically with the caller's transaction.
  - The per-artefact advisory lock that serialises index writers is acquired
    only on PostgreSQL (a no-op on the SQLite test DB) and never crashes the
    SQLite path — regression guard for the FOR-UPDATE -> advisory-lock change.
  - Re-running the same completed analysis is idempotent (delete-then-insert
    handler leaves exactly one set of rows).
  - A row with unparseable details is skipped without raising.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_search_index_populate -v
"""

import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-search-index-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _make_fixtures(db):
    """Platform -> Item -> Artefact, returning the artefact."""
    from arcology_shared.enums import ArtefactType
    from myapp.database import Artefact, Item, Platform

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
    return artefact


def _make_protection_analysis(db, artefact, indicators, *, details=None):
    """Create a COMPLETED DISC_PROTECTION_DETECT analysis on *artefact*."""
    from arcology_shared.enums import AnalysisType
    from myapp.database import Analysis, AnalysisStatus

    analysis = Analysis(
        artefact_id=artefact.id,
        analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
        status=AnalysisStatus.COMPLETED,
        success=True,
        details=details if details is not None
        else json.dumps({'indicators': indicators}),
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


class TestPopulateSearchIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        # Fresh fixtures per test; clear all rows first (FK-safe order) so the
        # unique platform/item names don't collide across tests.
        from myapp.database import Analysis, Artefact, ArtefactProtection, Item, Platform
        self.ctx = self.app.app_context()
        self.ctx.push()
        ArtefactProtection.query.delete()
        Analysis.query.delete()
        Artefact.query.delete()
        Item.query.delete()
        Platform.query.delete()
        self.db.session.commit()
        self.artefact = _make_fixtures(self.db)

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _protection_rows(self):
        from myapp.database import ArtefactProtection
        return ArtefactProtection.query.filter_by(
            artefact_id=self.artefact.id).all()

    def test_populates_and_commits_rows(self):
        """A completed analysis writes its index rows, visible after commit."""
        from myapp.services.search_index import populate_search_index_from_analysis

        analysis = _make_protection_analysis(
            self.db, self.artefact,
            [{'type': 'weak_bits', 'track': 5, 'side': 0}],
        )
        populate_search_index_from_analysis(analysis)
        self.db.session.commit()

        rows = self._protection_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].protection_type, 'weak_bits')
        self.assertEqual(rows[0].track, 5)

    def test_idempotent_rerun(self):
        """Re-running the same completed analysis leaves exactly one row set."""
        from myapp.services.search_index import populate_search_index_from_analysis

        analysis = _make_protection_analysis(
            self.db, self.artefact,
            [{'type': 'weak_bits', 'track': 5, 'side': 0}],
        )
        populate_search_index_from_analysis(analysis)
        self.db.session.commit()
        populate_search_index_from_analysis(analysis)
        self.db.session.commit()

        self.assertEqual(len(self._protection_rows()), 1)

    def test_unparseable_details_skipped(self):
        """A row with non-JSON details is skipped without raising."""
        from myapp.services.search_index import populate_search_index_from_analysis

        analysis = _make_protection_analysis(
            self.db, self.artefact, [], details='not-json{')
        # Must not raise.
        populate_search_index_from_analysis(analysis)
        self.db.session.commit()
        self.assertEqual(len(self._protection_rows()), 0)

    def test_advisory_lock_acquired_on_postgresql(self):
        """On PostgreSQL the per-artefact advisory lock is taken (keyed on the
        namespace + artefact id); the artefact row lock is not, decoupling index
        writes from the job-creation path.  The handler still runs and commits."""
        from myapp.services import search_index
        from myapp.services.search_index import populate_search_index_from_analysis

        analysis = _make_protection_analysis(
            self.db, self.artefact, [{'type': 'weak_bits'}])

        lock_params = []
        real_execute = self.db.session.execute

        def _spy(stmt, params=None, *args, **kwargs):
            # Intercept the advisory-lock statement so the simulated-PG branch
            # is exercised without SQLite trying to run a Postgres-only function.
            if 'pg_advisory_xact_lock' in str(stmt):
                lock_params.append(params)
                return mock.Mock()
            return real_execute(stmt, params, *args, **kwargs) if params is not None \
                else real_execute(stmt, *args, **kwargs)

        # Pretend we're on PostgreSQL so the advisory-lock branch runs.
        fake_bind = mock.Mock()
        fake_bind.dialect.name = 'postgresql'

        with mock.patch.object(self.db.session, 'get_bind', return_value=fake_bind), \
                mock.patch.object(self.db.session, 'execute', side_effect=_spy):
            populate_search_index_from_analysis(analysis)
        self.db.session.commit()

        self.assertEqual(len(lock_params), 1, 'expected exactly one advisory lock')
        self.assertEqual(lock_params[0], {
            'ns': search_index._SEARCH_INDEX_LOCK_NAMESPACE,
            'aid': self.artefact.id,
        })
        # Namespace must fit in signed int4 (the two-arg lock takes int4 keys).
        self.assertEqual(
            search_index._SEARCH_INDEX_LOCK_NAMESPACE & 0x7FFFFFFF,
            search_index._SEARCH_INDEX_LOCK_NAMESPACE,
        )
        # The handler still ran under the simulated-PG path.
        self.assertEqual(len(self._protection_rows()), 1)

    def test_no_advisory_lock_on_sqlite(self):
        """On the SQLite test DB no advisory lock is attempted (no-op branch)."""
        from myapp.services.search_index import populate_search_index_from_analysis

        analysis = _make_protection_analysis(
            self.db, self.artefact, [{'type': 'weak_bits'}])

        executed = []
        real_execute = self.db.session.execute

        def _spy(stmt, *args, **kwargs):
            executed.append(str(stmt))
            return real_execute(stmt, *args, **kwargs)

        with mock.patch.object(self.db.session, 'execute', side_effect=_spy):
            populate_search_index_from_analysis(analysis)
        self.db.session.commit()

        self.assertFalse(
            [s for s in executed if 'pg_advisory_xact_lock' in s], executed)
        # Index rows still written via the SQLite path.
        self.assertEqual(len(self._protection_rows()), 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
