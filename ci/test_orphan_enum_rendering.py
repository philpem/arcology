"""
Orphan-enum rendering regression tests.

artefact_type / analysis_type use the _TolerantEnum column type, which yields
None for a DB value absent from the Python enum (an orphan row left behind when
a feature-branch migration is downgraded without cleaning up its rows — e.g.
NSFW_SCAN after switching off that branch).

A previous fix guarded the JSON API serialiser, but the HTML views and several
log paths still dereferenced `.value` directly, so any page rendering an
orphan-typed artefact or analysis 500'd.  These tests insert orphan enum values
straight into the DB (bypassing the ORM) and assert the real view endpoints
still render (no 500), and that the enum_value() helper / filter degrade
gracefully.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_orphan_enum_rendering -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-orphan-enum-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestEnumValueHelper(unittest.TestCase):
    """The shared enum_value() helper tolerates None."""

    def test_returns_value_for_member(self):
        from arcology_shared.enums import ArtefactType
        from myapp.utils.enum_display import enum_value
        self.assertEqual(enum_value(ArtefactType.SCP), 'scp')

    def test_returns_default_for_none(self):
        from myapp.utils.enum_display import enum_value
        self.assertIsNone(enum_value(None))
        self.assertEqual(enum_value(None, 'unknown'), 'unknown')


class TestOrphanEnumRendering(unittest.TestCase):
    """Real view endpoints must render artefacts/analyses whose enum column
    holds a value the Python enum no longer knows about."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.app import create_app
        from myapp.database import Analysis, AnalysisStatus, Artefact, Item
        from myapp.extensions import db

        # PUBLIC_MODE lets the test client GET read-only views without a login.
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['PUBLIC_MODE'] = True
        cls.app.config['PUBLIC_DOWNLOADS'] = True
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            item = Item(name='Orphan Render Item')
            item.slug = 'orphan-render-item'
            db.session.add(item)
            db.session.commit()

            # A normal parent artefact, plus a derived child — exercises the
            # derived_artefacts recursion in items/view.html and the sidebar.
            parent = Artefact(
                item_id=item.id, label='Parent', artefact_type=ArtefactType.SCP,
                original_filename='p.scp', storage_path='p.scp',
            )
            parent.slug = 'parent'
            db.session.add(parent)
            db.session.commit()

            child = Artefact(
                item_id=item.id, label='Child', artefact_type=ArtefactType.HFE,
                original_filename='c.hfe', storage_path='c.hfe',
                parent_artefact_id=parent.id,
            )
            child.slug = 'child'
            db.session.add(child)
            db.session.commit()

            analysis = Analysis(
                artefact_id=parent.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.COMPLETED,
            )
            db.session.add(analysis)
            db.session.commit()

            # Strand the enum values: write types the Python enums don't define.
            db.session.execute(
                db.text('UPDATE artefacts SET artefact_type = :v WHERE id = :i'),
                {'v': 'NSFW_IMAGE', 'i': child.id},
            )
            db.session.execute(
                db.text('UPDATE analyses SET analysis_type = :v WHERE id = :i'),
                {'v': 'NSFW_SCAN', 'i': analysis.id},
            )
            db.session.commit()

            cls.item_id = item.id
            cls.item_uuid = item.uuid
            cls.parent_uuid = parent.uuid
            cls.child_uuid = child.uuid

        cls.client = cls.app.test_client()

    def _assert_renders(self, path):
        # follow_redirects so canonical-slug 301s land on the real rendered page
        # (a template crash there would surface as a 500 on the final response).
        resp = self.client.get(path, follow_redirects=True)
        self.assertNotEqual(
            resp.status_code, 500,
            f'{path} returned 500 (orphan enum crashed the template):\n'
            f'{resp.get_data(as_text=True)[:800]}',
        )
        # 200 (rendered) or 404 (visibility) are both acceptable — just not 500.
        self.assertIn(resp.status_code, (200, 404), f'{path} -> {resp.status_code}')

    def test_item_view_renders(self):
        # items/view.html: parent badge + recursive derived-type aggregation.
        self._assert_renders(f'/items/{self.item_uuid}')

    def test_orphan_child_artefact_view_renders(self):
        # artefacts/view.html: artefact_type badge for the orphan-typed child.
        self._assert_renders(f'/artefacts/{self.child_uuid}')

    def test_artefact_tree_renders(self):
        # artefacts/tree.html: art.artefact_type badge + analysis_type compare.
        self._assert_renders(f'/artefacts/{self.parent_uuid}/tree')

    def test_parent_artefact_view_renders(self):
        # Parent view embeds the orphan-typed analysis (analysis_type None).
        self._assert_renders(f'/artefacts/{self.parent_uuid}')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
