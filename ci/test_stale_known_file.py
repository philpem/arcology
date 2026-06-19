"""
Regression test: the artefact file listing must not crash on an extracted file
whose ``is_known`` flag is stale (True) but whose ``known_file_id`` is NULL.

This happens when a KnownFile is deleted: the ``known_file_id`` FK
(ON DELETE SET NULL) nulls the link but leaves ``is_known = True``.  The
file-listing template trusted ``is_known`` and then dereferenced
``file.known_file.database_id`` on ``None`` -> UndefinedError -> HTTP 500.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_stale_known_file -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-stale-known-file-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestStaleKnownFileRender(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
            StorageDirectory,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['PUBLIC_MODE'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            item = Item(name='Public Disc', is_private=False)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0,
                             label='Main', filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            # The stale state: flagged known, but the KnownFile is gone.
            db.session.add(ExtractedFile(
                partition_id=part.id, path='ORPHAN', filename='ORPHAN',
                file_size=1024, sha256='cc' * 32, is_directory=False,
                is_known=True, known_file_id=None))
            db.session.commit()
            cls.url = f'/items/{item.url_id}/artefacts/{art.uuid}'

    def test_view_renders_without_crash(self):
        r = self.client.get(self.url, follow_redirects=True)
        self.assertEqual(r.status_code, 200, r.data[:500])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
