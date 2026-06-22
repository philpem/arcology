"""
Regression test: adding extracted files to a hash database from the artefact
file-listing ("HashDB Mode") must not run the collection-linking pass inline.

Linking freshly-added KnownFiles against the whole extracted-file corpus (plus
the recognition backfill) is unbounded work.  Doing it synchronously in the
web request made a bulk add of hundreds of files blow past the reverse proxy's
timeout — the KnownFile rows committed, but the user saw a 504 instead of the
redirect.  The route now hands the work to the task runner by queueing a single
HASHDB_LINK job (exactly as the web DB-import route does), so the request
returns promptly.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_add_to_hashdb_async_link -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-add-to-hashdb-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_MD5 = 'ab' * 16
_SHA1 = 'cd' * 20


def _make_user(db, username):
    import bcrypt
    from myapp.database import User, UserPermission
    pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
    user = User(username=username, password_hash=pw,
                permission=UserPermission.READ_WRITE)
    db.session.add(user)
    db.session.flush()
    return user


class TestAddToHashdbAsyncLink(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownProduct,
            Partition,
            StorageDirectory,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.db = db

        with cls.app.app_context():
            db.create_all()
            user = _make_user(db, 'hashdb-adder')
            cls.user_id = user.id

            # Active database + empty product to add files into.
            hdb = HashDatabase(name='Target DB')
            db.session.add(hdb)
            db.session.flush()
            prod = KnownProduct(database_id=hdb.id, title='!Foo')
            db.session.add(prod)
            db.session.flush()
            cls.db_id, cls.product_id = hdb.id, prod.id

            # A public item + artefact with an extracted file (hash already
            # present, so the route does not need to read it off disk).
            item = Item(name='coll')
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS)
            db.session.add(art)
            db.session.flush()
            cls.artefact_uuid = art.uuid
            part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            ef = ExtractedFile(
                partition_id=part.id, path='!Foo/!RunImage', filename='!RunImage',
                md5=_MD5, sha1=_SHA1, file_size=123, is_directory=False)
            db.session.add(ef)
            db.session.flush()
            cls.ef_id = ef.id
            db.session.commit()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True

    def test_add_commits_files_and_queues_link_job(self):
        from arcology_shared.enums import AnalysisType
        from myapp.database import Analysis, AnalysisStatus, KnownFile

        resp = self.client.post(
            f'/{self.artefact_uuid}/add-to-hashdb',
            data={
                'file_ids': str(self.ef_id),
                'database_id': str(self.db_id),
                'product_id': str(self.product_id),
                'is_required': '1',
            },
            follow_redirects=False,
        )
        # The request returns a redirect (not a 504): the heavy linking work was
        # deferred, not run inline.
        self.assertEqual(resp.status_code, 302, resp.data)

        with self.app.app_context():
            # The KnownFile row was committed.
            kf = KnownFile.query.filter_by(database_id=self.db_id, md5=_MD5).first()
            self.assertIsNotNone(kf)

            # Exactly one HASHDB_LINK job was queued for this database, and the
            # inline rescan did NOT link the extracted file yet (that happens
            # when the task runner runs the job).
            link_jobs = (
                Analysis.query
                .filter_by(artefact_id=None,
                           analysis_type=AnalysisType.HASHDB_LINK,
                           status=AnalysisStatus.PENDING)
                .all()
            )
            self.assertEqual(len(link_jobs), 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
