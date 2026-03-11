"""
Tests for the CHECKSUM_COMPUTE analysis type.

Covers:
  - CHECKSUM_COMPUTE is not listed in ANALYSIS_MAP (it is implicit)
  - queue_analyses_for_artefact() always prepends CHECKSUM_COMPUTE first
  - checksum_only=True queues only CHECKSUM_COMPUTE
  - PATCH /api/artefacts/<uuid> endpoint updates md5/sha256

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_checksum_compute -v
"""

import os
import sys
import json
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-smoke-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']

from shared.enums import ArtefactType, AnalysisType
from myapp.blueprints.artefacts import ANALYSIS_MAP


class TestAnalysisMapDoesNotContainChecksum(unittest.TestCase):
    """CHECKSUM_COMPUTE must be implicit, not listed in ANALYSIS_MAP."""

    def test_checksum_compute_not_in_analysis_map(self):
        """CHECKSUM_COMPUTE should not appear in any ANALYSIS_MAP value list."""
        for artefact_type, analysis_list in ANALYSIS_MAP.items():
            self.assertNotIn(
                AnalysisType.CHECKSUM_COMPUTE,
                analysis_list,
                f'CHECKSUM_COMPUTE found in ANALYSIS_MAP[{artefact_type}]; '
                'it is prepended automatically by queue_analyses_for_artefact().',
            )


class TestQueueAnalysesForArtefact(unittest.TestCase):
    """queue_analyses_for_artefact() always prepends CHECKSUM_COMPUTE."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        from myapp.database import Item, Artefact

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            db.create_all()
            item = Item(name='Test Item')
            db.session.add(item)
            db.session.flush()
            artefact = Artefact(
                item_id=item.id,
                label='Test Artefact',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test.img',
                storage_path='test.img',
            )
            db.session.add(artefact)
            db.session.commit()
            cls.artefact_id = artefact.id

    def setUp(self):
        """Clear all Analysis rows before each test."""
        from myapp.extensions import db
        from myapp.database import Analysis

        with self.app.app_context():
            Analysis.query.delete()
            db.session.commit()

    def _queue(self, **kwargs):
        from myapp.database import Artefact
        from myapp.blueprints.artefacts import queue_analyses_for_artefact

        with self.app.app_context():
            from myapp.extensions import db
            artefact = db.session.get(Artefact, self.artefact_id)
            queue_analyses_for_artefact(artefact, **kwargs)

    def _get_queued_types(self):
        from myapp.database import Analysis

        with self.app.app_context():
            rows = Analysis.query.order_by(Analysis.id).all()
            return [r.analysis_type for r in rows]

    def test_checksum_always_queued_first(self):
        """CHECKSUM_COMPUTE must be the first queued analysis."""
        self._queue()
        queued = self._get_queued_types()
        self.assertTrue(queued, 'No analyses were queued')
        self.assertEqual(
            queued[0],
            AnalysisType.CHECKSUM_COMPUTE,
            f'First queued analysis was {queued[0]}, expected CHECKSUM_COMPUTE',
        )

    def test_checksum_only_queues_one_analysis(self):
        """checksum_only=True must queue exactly one analysis: CHECKSUM_COMPUTE."""
        self._queue(checksum_only=True)
        queued = self._get_queued_types()
        self.assertEqual(len(queued), 1, f'Expected 1 analysis, got {len(queued)}: {queued}')
        self.assertEqual(queued[0], AnalysisType.CHECKSUM_COMPUTE)

    def test_type_specific_analyses_queued_with_checksum(self):
        """Without checksum_only, type-specific analyses follow CHECKSUM_COMPUTE."""
        self._queue(checksum_only=False)
        queued = self._get_queued_types()
        self.assertGreater(len(queued), 1, 'Expected more than one analysis to be queued')
        self.assertEqual(queued[0], AnalysisType.CHECKSUM_COMPUTE)

    def test_no_duplicate_checksum_on_re_queue(self):
        """Calling queue_analyses_for_artefact twice does not duplicate CHECKSUM_COMPUTE."""
        self._queue(checksum_only=True)
        self._queue(checksum_only=True)
        queued = self._get_queued_types()
        checksum_count = sum(1 for t in queued if t == AnalysisType.CHECKSUM_COMPUTE)
        self.assertEqual(checksum_count, 1, 'CHECKSUM_COMPUTE was queued more than once')


class TestPatchArtefactEndpoint(unittest.TestCase):
    """PATCH /api/artefacts/<uuid> updates md5 and sha256."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        from myapp.database import Item, Artefact

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.create_all()
            item = Item(name='Hash Test Item')
            db.session.add(item)
            db.session.flush()
            artefact = Artefact(
                item_id=item.id,
                label='Hash Test Artefact',
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='hash_test.img',
                storage_path='hash_test.img',
            )
            db.session.add(artefact)
            db.session.commit()
            cls.artefact_uuid = artefact.uuid

    def _auth_headers(self):
        return {'X-API-Key': _WORKER_KEY, 'Content-Type': 'application/json'}

    def test_patch_returns_401_without_auth(self):
        resp = self.client.patch(
            f'/api/artefacts/{self.artefact_uuid}',
            data=json.dumps({'md5': 'abc'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_patch_returns_404_for_unknown_uuid(self):
        resp = self.client.patch(
            '/api/artefacts/00000000000000000000000000000000',
            data=json.dumps({'md5': 'abc'}),
            content_type='application/json',
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_patch_updates_md5_and_sha256(self):
        md5_val = 'a' * 32
        sha256_val = 'b' * 64
        resp = self.client.patch(
            f'/api/artefacts/{self.artefact_uuid}',
            data=json.dumps({'md5': md5_val, 'sha256': sha256_val}),
            content_type='application/json',
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.get_json()
        self.assertEqual(data.get('md5'), md5_val)
        self.assertEqual(data.get('sha256'), sha256_val)

    def test_patch_is_idempotent(self):
        payload = json.dumps({'md5': 'c' * 32, 'sha256': 'd' * 64})
        headers = self._auth_headers()
        url = f'/api/artefacts/{self.artefact_uuid}'
        resp1 = self.client.patch(url, data=payload, content_type='application/json', headers=headers)
        resp2 = self.client.patch(url, data=payload, content_type='application/json', headers=headers)
        self.assertEqual(resp1.status_code, 200, resp1.data)
        self.assertEqual(resp2.status_code, 200, resp2.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
