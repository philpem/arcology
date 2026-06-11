"""
Tests that GET /api/analysis/pending is restricted to the worker.

This endpoint is the worker's job-polling endpoint: it returns *every* pending
analysis system-wide, including each artefact's storage_path and its item's
uuid/slug, with no per-artefact visibility filter (the worker must see private
content to analyse it).  It must therefore be reachable only by the worker's
pre-shared key — an ordinary user API key (even read_write) could otherwise
enumerate private artefacts and their storage locations.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_pending_analyses_auth -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-pending-auth-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestPendingAnalysesAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            ApiKey,
            ApiKeyPermission,
            Artefact,
            Item,
            User,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            u = User(username='pend-user', password_hash='x',
                     permission=UserPermission.READ_WRITE, can_use_api=True)
            db.session.add(u)
            db.session.flush()
            key, cls.user_key = ApiKey.create(user_id=u.id, name='k',
                                              permission=ApiKeyPermission.READ_WRITE)
            db.session.add(key)

            # A PRIVATE item + artefact with a pending analysis, whose storage
            # path and item slug must not leak to a user key.
            item = Item(name='secret-item', is_private=True, owner_id=u.id)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='secret-art',
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='s.img',
                           storage_path='uploads/SECRET-STORAGE-PATH.img')
            db.session.add(art)
            db.session.flush()
            db.session.add(Analysis(artefact_id=art.id,
                                    analysis_type=AnalysisType.METADATA_EXTRACT,
                                    status=AnalysisStatus.PENDING))
            db.session.commit()

    def test_user_key_forbidden(self):
        r = self.client.get('/api/analysis/pending', headers={'X-API-Key': self.user_key})
        self.assertEqual(r.status_code, 403, r.data)
        # Nothing about the private artefact leaks in the body.
        self.assertNotIn(b'SECRET-STORAGE-PATH', r.data)
        self.assertNotIn(b'secret-art', r.data)

    def test_worker_key_allowed(self):
        r = self.client.get('/api/analysis/pending', headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(r.status_code, 200, r.data)
        # The worker is trusted and does receive the storage path it needs.
        self.assertIn(b'SECRET-STORAGE-PATH', r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
