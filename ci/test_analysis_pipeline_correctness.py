"""
Analysis-pipeline correctness regressions.

* add_files truncates over-length worker-supplied strings to their column width.
  On PostgreSQL an over-length INSERT raises "value too long" and the worker
  retries the same batch forever until it dead-letters; SQLite silently accepts
  it, so this behaviour would ship untested without an explicit check.

* The cancel route deletes a pending analysis only if it is STILL pending at
  write time (rowcount-guarded), so a worker that atomically claims the job
  between the status read and the delete can't have a running job deleted out
  from under it.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_analysis_pipeline_correctness -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-pipeline-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']
_AUTH = {'X-API-Key': _WORKER_KEY}


class TestAddFilesTruncation(unittest.TestCase):
    """add_files caps worker strings to their DB column width."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import Artefact, FilesystemType, Item, Partition
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            item = Item(name='item')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='disc',
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='d.img', storage_path='uploads/d.img')
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0,
                             filesystem=FilesystemType.UNKNOWN)
            db.session.add(part)
            db.session.commit()
            cls.partition_uuid = part.uuid

    def test_overlong_path_and_attributes_are_truncated(self):
        long_path = 'A' * 1500          # column is String(1000)
        long_attrs = 'W' * 200          # column is String(50)
        resp = self.client.post(
            f'/api/partitions/{self.partition_uuid}/files',
            json={'files': [{
                'path': long_path,
                'filename': 'F' * 400,   # column is String(255)
                'attributes': long_attrs,
            }]},
            headers=_AUTH,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            from myapp.database import ExtractedFile, Partition
            part = Partition.query.filter_by(uuid=self.partition_uuid).first()
            ef = ExtractedFile.query.filter_by(partition_id=part.id).first()
            self.assertIsNotNone(ef)
            self.assertEqual(len(ef.path), 1000)
            self.assertEqual(len(ef.filename), 255)
            self.assertEqual(len(ef.attributes), 50)


class TestCancelRaceGuard(unittest.TestCase):
    """Cancelling a job that has since started running is a no-op."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import Artefact, Item, User, UserPermission
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            admin = User(username='pipe-admin', password_hash='x', is_admin=True,
                         permission=UserPermission.READ_WRITE)
            db.session.add(admin)
            item = Item(name='item', owner_id=None)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='disc',
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='d.img', storage_path='uploads/d2.img')
            db.session.add(art)
            db.session.flush()
            db.session.commit()
            cls.admin_id = admin.id
            cls.artefact_id = art.id

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def _make_analysis(self, status):
        from arcology_shared.enums import AnalysisType
        from myapp.database import Analysis, AnalysisStatus
        with self.app.app_context():
            a = Analysis(artefact_id=self.artefact_id,
                         analysis_type=AnalysisType.METADATA_EXTRACT,
                         status=getattr(AnalysisStatus, status))
            self.db.session.add(a)
            self.db.session.commit()
            return a.uuid, a.id

    def test_cancel_pending_deletes(self):
        self._login()
        uuid, aid = self._make_analysis('PENDING')
        resp = self.client.post(f'/analysis/{uuid}/cancel', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            from myapp.database import Analysis
            self.assertIsNone(self.db.session.get(Analysis, aid))

    def test_cancel_running_is_a_noop(self):
        # A job that a worker has already claimed (RUNNING) must not be deleted
        # by a cancel that raced the claim. _require_analysis_status rejects it
        # up front here; the rowcount guard is the backstop for the TOCTOU where
        # the claim lands after that check.
        self._login()
        uuid, aid = self._make_analysis('RUNNING')
        self.client.post(f'/analysis/{uuid}/cancel', follow_redirects=False)
        with self.app.app_context():
            from myapp.database import Analysis
            self.assertIsNotNone(self.db.session.get(Analysis, aid),
                                 'a running analysis must survive a cancel')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
