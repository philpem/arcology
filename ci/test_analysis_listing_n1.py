"""
Regression test: the /analysis listing must not issue per-row lazy queries.

The template's artefact_url() reads artefact.item.url_id and
artefact.root_artefact (which walks parent_artefact), so without eager-loading
those relationships each listed analysis row triggered extra SELECTs — an N+1
that scaled with the page size (up to 10 000 under "view all").

This measures the SELECT count for the page with a small vs a larger number of
analyses (each on a DISTINCT derived artefact, so the identity map can't mask
the N+1) and asserts the count barely grows — i.e. it's O(1) in the row count,
not O(n).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_listing_n1 -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-analysis-n1-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestAnalysisListingN1(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import User, UserPermission
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            db.create_all()
            viewer = User(username='n1-viewer', password_hash='x',
                          permission=UserPermission.READ_ONLY)
            db.session.add(viewer)
            db.session.commit()
            cls.viewer_id = viewer.id

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.viewer_id)
            sess['_fresh'] = True

    def _seed_analyses(self, n, start):
        """Create *n* completed analyses, each on its own derived artefact under
        its own public item (so no two share an item/parent in the identity map)."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            Artefact,
            Item,
        )
        with self.app.app_context():
            for i in range(start, start + n):
                item = Item(name=f'n1-item-{i}')
                self.db.session.add(item)
                self.db.session.flush()
                root = Artefact(item_id=item.id, label=f'root-{i}',
                                artefact_type=ArtefactType.HFE,
                                original_filename=f'r{i}.hfe', storage_path=f'r{i}.hfe')
                self.db.session.add(root)
                self.db.session.flush()
                derived = Artefact(item_id=item.id, label=f'derived-{i}',
                                   artefact_type=ArtefactType.RAW_SECTOR,
                                   original_filename=f'd{i}.img', storage_path=f'd{i}.img',
                                   parent_artefact_id=root.id)
                self.db.session.add(derived)
                self.db.session.flush()
                self.db.session.add(Analysis(artefact_id=derived.id,
                                             analysis_type=AnalysisType.METADATA_EXTRACT,
                                             status=AnalysisStatus.COMPLETED, success=True))
            self.db.session.commit()

    def _count_select_queries_for_index(self):
        from sqlalchemy import event
        with self.app.app_context():
            engine = self.db.engine
        count = {'n': 0}

        def _before(conn, cursor, statement, params, context, executemany):
            if statement.lstrip()[:6].upper() == 'SELECT':
                count['n'] += 1

        event.listen(engine, 'before_cursor_execute', _before)
        try:
            self._login()
            resp = self.client.get('/analysis/?per_page=all')
            self.assertEqual(resp.status_code, 200, resp.data)
        finally:
            event.remove(engine, 'before_cursor_execute', _before)
        return count['n']

    def test_listing_query_count_is_flat_in_row_count(self):
        self._seed_analyses(4, start=0)
        small = self._count_select_queries_for_index()
        self._seed_analyses(8, start=100)   # 12 rows total now
        large = self._count_select_queries_for_index()
        # Tripling the rows must not triple the queries.  Eager loading keeps the
        # delta near zero; an artefact/item/parent N+1 would add ~2 per new row
        # (~16 here), so a small ceiling cleanly distinguishes the two.
        self.assertLessEqual(large - small, 3,
                             f'query count grew {small} -> {large} with more rows (N+1?)')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
