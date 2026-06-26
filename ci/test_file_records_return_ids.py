"""The file-registration endpoint returns the created files' ids.

POST /api/partitions/{uuid}/files now returns a ``files`` list of ``{id, path}``
for every incoming file — both freshly added and pre-existing (so a retried
registration still gets ids).  The worker relies on this to fold archive
detection into registration (queue ARCHIVE_EXTRACT with the file id) instead of
running a separate ARCHIVE_DETECT scan.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_file_records_return_ids -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestFileRecordsReturnIds(unittest.TestCase):
    def setUp(self):
        from myapp.app import create_app
        from myapp.extensions import db
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.db = db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _partition(self):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item, StorageDirectory
        db = self.db
        item = Item(name='disc', is_private=False)
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.commit()
        hdr = {'X-API-Key': _WORKER_KEY}
        resp = self.client.post(
            f'/api/artefacts/{art.uuid}/partitions',
            json={'partition_index': 0, 'filesystem': 'dfs'}, headers=hdr)
        return resp.get_json()['uuid'], hdr

    def test_returns_id_for_each_added_file(self):
        from myapp.database import ExtractedFile
        puuid, hdr = self._partition()
        files = [{'path': 'a.zip', 'filename': 'a.zip', 'is_directory': False},
                 {'path': 'docs/readme', 'filename': 'readme', 'is_directory': False}]
        resp = self.client.post(f'/api/partitions/{puuid}/files',
                                json={'files': files}, headers=hdr)
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        returned = {e['path']: e['id'] for e in body['files']}
        self.assertEqual(set(returned), {'a.zip', 'docs/readme'})
        # ids are the real DB row ids
        for path, fid in returned.items():
            row = self.db.session.get(ExtractedFile, fid)
            self.assertIsNotNone(row)
            self.assertEqual(row.path, path)

    def test_returns_id_for_preexisting_file_on_retry(self):
        # A re-posted (deduped) file is reported with its existing id, so a
        # retried registration can still detect archives among already-present
        # rows.
        puuid, hdr = self._partition()
        files = [{'path': 'a.zip', 'filename': 'a.zip', 'is_directory': False}]
        first = self.client.post(f'/api/partitions/{puuid}/files',
                                 json={'files': files}, headers=hdr).get_json()
        again = self.client.post(f'/api/partitions/{puuid}/files',
                                 json={'files': files}, headers=hdr).get_json()
        self.assertEqual(again.get('skipped'), 1)        # deduped
        self.assertEqual(again['files'], first['files'])  # same id reported


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
