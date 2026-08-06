"""
Regression test: POST /api/partitions/{uuid}/files must not issue one query per
incoming file to resolve archive parents.

The endpoint already batched its duplicate guard and its known-file matching,
but the parent-archive lookup used to run ``db.session.get(ExtractedFile, ...)``
inside the per-file loop.  Every file in an ARCHIVE_EXTRACT batch carries a
parent_file_id, so that fired once per file -- up to the worker's batch_size of
100 per request, and ~20 000 point SELECTs spread over 200 requests for a large
nested archive.

It went unnoticed because the endpoint stayed *correct* and each query was
individually fast; it showed up as this endpoint averaging ~46 database spans
per request in Sentry against a ~4 baseline for everything else.

This measures the SELECT count for a small vs a larger batch of files that all
nest under the same archive, and asserts the count doesn't scale with the batch
size.  Distinct parents are used in a second case so the ORM identity map can't
mask a reintroduced N+1.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_add_files_n1 -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')
os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-add-files-n1-test-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestAddFilesN1(unittest.TestCase):

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
        self.hdr = {'X-API-Key': _WORKER_KEY}
        self.puuid = self._partition()

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
        resp = self.client.post(
            f'/api/artefacts/{art.uuid}/partitions',
            json={'partition_index': 0, 'filesystem': 'dfs'}, headers=self.hdr)
        return resp.get_json()['uuid']

    def _make_archive(self, name):
        """Register one file and mark it as an archive; return its row id."""
        body = self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [{'path': name, 'filename': name,
                             'is_directory': False}]},
            headers=self.hdr).get_json()
        fid = body['files'][0]['id']
        # mark_archive requires a JSON object body (_json_object(required=True)).
        resp = self.client.post(f'/api/files/{fid}/mark_archive',
                                json={'is_archive': True}, headers=self.hdr)
        self.assertEqual(resp.status_code, 200, resp.data)
        return fid

    def _count_selects_for_post(self, files):
        from sqlalchemy import event
        count = {'n': 0}
        engine = self.db.engine

        def _before(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                count['n'] += 1

        event.listen(engine, 'before_cursor_execute', _before)
        try:
            resp = self.client.post(f'/api/partitions/{self.puuid}/files',
                                    json={'files': files}, headers=self.hdr)
            self.assertEqual(resp.status_code, 200, resp.data)
        finally:
            event.remove(engine, 'before_cursor_execute', _before)
        return count['n']

    def _children(self, parent_id, n, tag):
        return [{'path': f'{tag}/f{i}.dat', 'filename': f'f{i}.dat',
                 'is_directory': False, 'parent_file_id': parent_id}
                for i in range(n)]

    def test_query_count_is_flat_in_batch_size(self):
        """One shared parent: 4 children vs 40 must cost about the same."""
        parent = self._make_archive('one.zip')
        small = self._count_selects_for_post(self._children(parent, 4, 'a'))
        large = self._count_selects_for_post(self._children(parent, 40, 'b'))
        # Batched, the delta is ~0.  A per-file lookup would add 36 SELECTs.
        self.assertLessEqual(
            large - small, 3,
            f'SELECT count grew {small} -> {large} for 4 -> 40 files (N+1?)')

    def test_query_count_is_flat_with_distinct_parents(self):
        """Distinct parents, so the identity map cannot hide a per-file get()."""
        few = [self._make_archive(f'few{i}.zip') for i in range(2)]
        many = [self._make_archive(f'many{i}.zip') for i in range(20)]

        small = self._count_selects_for_post(
            [self._children(p, 1, f'p{i}')[0] for i, p in enumerate(few)])
        large = self._count_selects_for_post(
            [self._children(p, 1, f'q{i}')[0] for i, p in enumerate(many)])
        self.assertLessEqual(
            large - small, 3,
            f'SELECT count grew {small} -> {large} for 2 -> 20 parents (N+1?)')

    def test_nesting_behaviour_is_unchanged(self):
        """The batching must not change where files actually land."""
        from myapp.database import ExtractedFile
        parent = self._make_archive('nest.zip')
        resp = self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [
                {'path': 'inner.dat', 'filename': 'inner.dat',
                 'is_directory': False, 'parent_file_id': parent},
                # No parent: must NOT be nested.
                {'path': 'loose.dat', 'filename': 'loose.dat',
                 'is_directory': False},
                # Already prefixed: must not be double-nested.
                {'path': 'nest.zip/pre.dat', 'filename': 'pre.dat',
                 'is_directory': False, 'parent_file_id': parent},
            ]}, headers=self.hdr)
        self.assertEqual(resp.status_code, 200, resp.data)

        paths = {r.path for r in self.db.session.query(ExtractedFile.path).all()}
        self.assertIn('nest.zip/inner.dat', paths)
        self.assertIn('loose.dat', paths)
        self.assertIn('nest.zip/pre.dat', paths)
        self.assertNotIn('nest.zip/nest.zip/pre.dat', paths)

    def test_unknown_parent_id_is_ignored_not_fatal(self):
        """A dangling parent_file_id must behave as before: no nesting, no 500."""
        from myapp.database import ExtractedFile
        resp = self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [{'path': 'orphan.dat', 'filename': 'orphan.dat',
                             'is_directory': False,
                             'parent_file_id': 999999}]}, headers=self.hdr)
        self.assertEqual(resp.status_code, 200, resp.data)
        paths = {r.path for r in self.db.session.query(ExtractedFile.path).all()}
        self.assertIn('orphan.dat', paths)

    def test_non_archive_parent_does_not_nest(self):
        """Only parents flagged is_archive nest their children."""
        from myapp.database import ExtractedFile
        body = self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [{'path': 'plain.dat', 'filename': 'plain.dat',
                             'is_directory': False}]},
            headers=self.hdr).get_json()
        plain_id = body['files'][0]['id']   # never marked as an archive

        self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [{'path': 'child.dat', 'filename': 'child.dat',
                             'is_directory': False,
                             'parent_file_id': plain_id}]}, headers=self.hdr)
        paths = {r.path for r in self.db.session.query(ExtractedFile.path).all()}
        self.assertIn('child.dat', paths)
        self.assertNotIn('plain.dat/child.dat', paths)


class TestAddFilesDeadlockPath(TestAddFilesN1):
    """The INSERTs moved from commit() into an explicit flush().

    Both sit inside the same try/except, but the deadlock handler is worth
    pinning down: on PostgreSQL a deadlock now surfaces from flush() rather than
    commit(), and the worker depends on getting a 503 (retryable) instead of a
    500.  SQLite raises no pgcode, so the error is injected.
    """

    @staticmethod
    def _db_error(pgcode):
        from sqlalchemy.exc import OperationalError

        class _Orig(Exception):
            pass

        _Orig.pgcode = pgcode
        return OperationalError('INSERT INTO extracted_files ...', {}, _Orig())

    @staticmethod
    def _failing_flush(err):
        """Patch Session.flush to fail only on the endpoint's explicit flush.

        A blanket patch is no good: SQLAlchemy's autoflush calls flush() before
        each query regardless of whether anything is pending, so the error would
        escape from a read long before reaching the try/except under test.
        Raising only once new ExtractedFile rows are pending pinpoints the
        flush() that stands in for the INSERTs.
        """
        from unittest import mock
        from sqlalchemy.orm import Session
        from myapp.database import ExtractedFile

        real_flush = Session.flush

        def _flush(self, *args, **kwargs):
            if any(isinstance(o, ExtractedFile) for o in self.new):
                raise err
            return real_flush(self, *args, **kwargs)

        return mock.patch.object(Session, 'flush', _flush)

    def test_deadlock_at_flush_returns_503(self):
        files = [{'path': 'd.dat', 'filename': 'd.dat', 'is_directory': False}]
        with self._failing_flush(self._db_error('40P01')):
            resp = self.client.post(f'/api/partitions/{self.puuid}/files',
                                    json={'files': files}, headers=self.hdr)
        self.assertEqual(resp.status_code, 503, resp.data)

    def test_session_is_usable_after_a_deadlock(self):
        """The rollback must leave the pooled session clean for the next request.

        Without it the following request fails with "this Session's transaction
        has been rolled back" -- which would turn one deadlock into a stuck
        worker rather than a retry.
        """
        from myapp.database import ExtractedFile
        with self._failing_flush(self._db_error('40P01')):
            self.client.post(
                f'/api/partitions/{self.puuid}/files',
                json={'files': [{'path': 'boom.dat', 'filename': 'boom.dat',
                                 'is_directory': False}]}, headers=self.hdr)

        # The retry the worker would make.
        resp = self.client.post(
            f'/api/partitions/{self.puuid}/files',
            json={'files': [{'path': 'after.dat', 'filename': 'after.dat',
                             'is_directory': False}]}, headers=self.hdr)
        self.assertEqual(resp.status_code, 200, resp.data)
        paths = {r.path for r in self.db.session.query(ExtractedFile.path).all()}
        self.assertIn('after.dat', paths)
        # The rolled-back batch must not have been partially persisted.
        self.assertNotIn('boom.dat', paths)

    def test_non_deadlock_operational_error_still_raises(self):
        """Only 40P01 is retryable; anything else must not be masked as a 503."""
        from sqlalchemy.exc import OperationalError
        # 42P01 is UndefinedTable — a real bug, not contention.
        with self._failing_flush(self._db_error('42P01')):
            with self.assertRaises(OperationalError):
                self.client.post(
                    f'/api/partitions/{self.puuid}/files',
                    json={'files': [{'path': 'x.dat', 'filename': 'x.dat',
                                     'is_directory': False}]}, headers=self.hdr)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
