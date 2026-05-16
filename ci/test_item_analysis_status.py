"""
Tests for the per-root-artefact analysis status aggregation used by the
item view.

Verifies the priority ordering (RUNNING > FAILED > PENDING > COMPLETED),
that artefacts with no analyses are absent from the result, and that
analyses on descendant artefacts roll up to their root.
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestArtefactAnalysisStatus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        with cls.app.app_context():
            db.create_all()

    def _make_artefact(self, item, label, parent=None):
        from myapp.database import Artefact
        from myapp.extensions import db
        from shared.enums import ArtefactType

        artefact = Artefact(
            item_id=item.id,
            label=label,
            artefact_type=ArtefactType.UNKNOWN,
            original_filename=f'{label}.bin',
            storage_path=f'{label}.bin',
            parent_artefact_id=parent.id if parent else None,
        )
        db.session.add(artefact)
        db.session.flush()
        return artefact

    def _add_analyses(self, artefact, statuses):
        from myapp.database import Analysis, AnalysisStatus
        from myapp.extensions import db
        from shared.enums import AnalysisType

        for s in statuses:
            db.session.add(Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus[s],
            ))
        db.session.commit()

    def _make_artefact_with_analyses(self, item, label, statuses, parent=None):
        artefact = self._make_artefact(item, label, parent=parent)
        self._add_analyses(artefact, statuses)
        return artefact

    def test_priority_and_counts(self):
        from myapp.blueprints.items import _compute_artefact_analysis_status
        from myapp.database import Item
        from myapp.extensions import db

        with self.app.app_context():
            item = Item(name='holder')
            db.session.add(item)
            db.session.flush()

            running = self._make_artefact_with_analyses(
                item, 'running-wins', ['COMPLETED', 'FAILED', 'PENDING', 'RUNNING'],
            )
            failed = self._make_artefact_with_analyses(
                item, 'failed-wins', ['COMPLETED', 'PENDING', 'FAILED'],
            )
            pending = self._make_artefact_with_analyses(
                item, 'pending-wins', ['COMPLETED', 'PENDING'],
            )
            done = self._make_artefact_with_analyses(
                item, 'done', ['COMPLETED', 'COMPLETED'],
            )
            empty = self._make_artefact_with_analyses(item, 'no-analyses', [])

            ids = [running.id, failed.id, pending.id, done.id, empty.id]
            result = _compute_artefact_analysis_status(ids)

        self.assertEqual(result[running.id], ('running', 1, 4))
        self.assertEqual(result[failed.id], ('failed', 1, 3))
        self.assertEqual(result[pending.id], ('pending', 1, 2))
        self.assertEqual(result[done.id], ('completed', 2, 2))
        self.assertNotIn(empty.id, result)

    def test_descendant_analyses_roll_up_to_root(self):
        """A root artefact's status reflects its descendants' analyses too."""
        from myapp.blueprints.items import _compute_artefact_analysis_status
        from myapp.database import Item
        from myapp.extensions import db

        with self.app.app_context():
            item = Item(name='roller')
            db.session.add(item)
            db.session.flush()

            # Root is fully done on its own, but a grandchild is still running.
            root = self._make_artefact_with_analyses(item, 'root', ['COMPLETED'])
            child = self._make_artefact_with_analyses(
                item, 'child', ['COMPLETED'], parent=root,
            )
            self._make_artefact_with_analyses(
                item, 'grandchild', ['RUNNING'], parent=child,
            )

            result = _compute_artefact_analysis_status([root.id])

        # Root rolls up: 3 total analyses, one running -> 'running' wins.
        self.assertEqual(result[root.id], ('running', 1, 3))

    def test_empty_input_returns_empty_dict(self):
        from myapp.blueprints.items import _compute_artefact_analysis_status

        with self.app.app_context():
            self.assertEqual(_compute_artefact_analysis_status([]), {})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
