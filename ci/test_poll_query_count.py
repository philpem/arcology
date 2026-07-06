"""
Worker-poll N+1 regression.

GET /api/analysis/pending serialises each analysis with its artefact's item,
owner, blob, tags and restrictions.  Without eager-loading those, the
serialisation lazy-loads ~5 relationships per row, so a busy queue polled by
several workers fired tens of thousands of point queries a minute.  This test
asserts the endpoint issues the SAME number of SQL statements for a large queue
as for a small one — i.e. no per-row scaling.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_poll_query_count -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-poll-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_AUTH = {'X-API-Key': os.environ['WORKER_API_KEY']}


class TestPollQueryCount(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import itertools
        from myapp.app import create_app
        from myapp.extensions import db
        cls._seq = itertools.count()
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def _make_pending(self, n):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            Artefact,
            ArtefactRestriction,
            Item,
            RestrictionType,
            Tag,
            User,
            UserPermission,
            artefact_tags,
        )
        with self.app.app_context():
            ArtefactRestriction.query.delete()
            self.db.session.execute(artefact_tags.delete())
            Analysis.query.delete()
            Artefact.query.delete()
            Tag.query.delete()
            self.db.session.commit()
            owner = User.query.filter_by(username='poll-owner').first()
            if owner is None:
                owner = User(username='poll-owner', password_hash='x',
                             permission=UserPermission.READ_WRITE)
                self.db.session.add(owner)
                self.db.session.flush()
            item = Item(name='poll-item', owner_id=owner.id)
            self.db.session.add(item)
            self.db.session.flush()
            for _ in range(n):
                seq = next(self._seq)
                art = Artefact(item_id=item.id, label=f'a{seq}',
                               artefact_type=ArtefactType.RAW_SECTOR,
                               original_filename=f'a{seq}.img',
                               storage_path=f'uploads/a{seq}.img', owner_id=owner.id)
                self.db.session.add(art)
                self.db.session.flush()
                art.tags.append(Tag(name=f'tag{seq}'))
                self.db.session.add(ArtefactRestriction(
                    artefact_id=art.id, restriction_type=RestrictionType.EXPLICIT))
                self.db.session.add(Analysis(
                    artefact_id=art.id, analysis_type=AnalysisType.METADATA_EXTRACT,
                    status=AnalysisStatus.PENDING))
            self.db.session.commit()

    def _count_poll_queries(self):
        from sqlalchemy import event
        statements = []

        def _before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        with self.app.app_context():
            engine = self.db.engine
        event.listen(engine, 'before_cursor_execute', _before)
        try:
            resp = self.client.get('/api/analysis/pending', headers=_AUTH)
        finally:
            event.remove(engine, 'before_cursor_execute', _before)
        self.assertEqual(resp.status_code, 200, resp.data)
        return len(statements), len(resp.get_json()['analyses'])

    def test_poll_query_count_is_constant_in_queue_size(self):
        self._make_pending(2)
        small_count, small_rows = self._count_poll_queries()
        self.assertEqual(small_rows, 2)

        self._make_pending(8)
        large_count, large_rows = self._count_poll_queries()
        self.assertEqual(large_rows, 8)

        # Eager-loading makes the statement count independent of the row count.
        # On the old lazy-loading path large_count would exceed small_count by
        # ~5 queries per extra row.
        self.assertEqual(
            small_count, large_count,
            f'poll issued {large_count} queries for 8 rows vs {small_count} for '
            f'2 — relationships are not being eager-loaded (N+1)')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
