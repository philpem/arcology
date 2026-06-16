"""
Regression guard against SQLAlchemy cartesian-product joins.

A query that joins tables without a join condition emits an ``SAWarning``
("SELECT statement has a cartesian product between FROM element(s)...") rather
than raising — so it is easy to ship unnoticed.  The CI runner
(``ci/run_app_tests.py``) escalates ``SAWarning`` to an error globally, but this
test makes the protection explicit and self-documenting for the aggregate /
count queries that are the usual source of the bug class (see the
"visibility-filter omission on aggregate queries" note in CLAUDE.md): it runs
them and asserts no ``SAWarning`` is emitted, independent of that escalation.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_cartesian_join -v
"""

import contextlib
import os
import sys
import unittest
import warnings
from sqlalchemy.exc import SAWarning

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-cartesian-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


@contextlib.contextmanager
def assert_no_cartesian_product(testcase):
    """Fail the test if any SAWarning is emitted inside the block.

    ``catch_warnings`` saves/restores the global filter state and showwarning
    hook, so this is robust whether or not the CI escalation is active and
    whether or not ``install_warning_capture()`` has run.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        yield
    sa_warnings = [w for w in caught if issubclass(w.category, SAWarning)]
    testcase.assertEqual(
        sa_warnings, [],
        'SQLAlchemy emitted SAWarning(s) (likely a cartesian-product join): '
        + '; '.join(f'{w.category.__name__}: {w.message}' for w in sa_warnings),
    )


class TestCartesianJoinGuard(unittest.TestCase):
    """Aggregate/count queries must not produce cartesian-product joins."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db

        with cls.app.app_context():
            _db.create_all()
            cls._seed_fixtures()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()

    @classmethod
    def _seed_fixtures(cls):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item, Platform

        platform = Platform(name='Cartesian Test Platform')
        cls.db.session.add(platform)
        cls.db.session.flush()

        item = Item(name='Cartesian Test Item', platform_id=platform.id)
        cls.db.session.add(item)
        cls.db.session.flush()

        artefact = Artefact(
            item_id=item.id,
            label='Cartesian Test Artefact',
            artefact_type=ArtefactType.RAW_SECTOR,
            original_filename='test.img',
            storage_path='uploads/cartesian_test.img',
        )
        cls.db.session.add(artefact)
        cls.db.session.flush()

        cls.db.session.add(Analysis(
            artefact_id=artefact.id,
            analysis_type=AnalysisType.CHECKSUM_COMPUTE,
            status=AnalysisStatus.PENDING,
        ))
        cls.db.session.commit()

    def _make_user(self, *, is_admin):
        """An admin (trivial visibility clauses) and a non-admin (real joins)
        exercise both branches of ``_get_stats``."""
        from myapp.database import User
        from myapp.enums import UserPermission
        user = User(username=f'cartesian-{"admin" if is_admin else "patron"}',
                    is_admin=is_admin,
                    permission=UserPermission.READ_WRITE)
        user.setPassword('x' * 12)
        return user

    def test_dashboard_stats_admin_no_cartesian(self):
        from myapp.blueprints.dashboard import _get_stats
        with self.app.app_context(), assert_no_cartesian_product(self):
            _get_stats(self._make_user(is_admin=True))

    def test_dashboard_stats_non_admin_no_cartesian(self):
        # The non-admin path adds the Artefact->Item joins that visibility
        # filtering requires -- the prime spot for a missing join condition.
        from myapp.blueprints.dashboard import _get_stats
        with self.app.app_context(), assert_no_cartesian_product(self):
            _get_stats(self._make_user(is_admin=False))


# vim: ts=4 sw=4 et
