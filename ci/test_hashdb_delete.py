"""
Deleting a hash database is offloaded to a bounded-step HASHDB_DELETE job
(GitHub issue #618, "Deleting hash databases takes a very long time", and the
follow-up that the inline bulk delete still hung the web thread on a
collection-scale database).

The web delete route only soft-deletes (is_deleting=True, is_active=False) and
queues the reap job; the task runner drains the rows in small, lock-friendly
batches via ``delete_one_step`` (myapp/services/hashdb_jobs.py), in-process with
direct DB access (no HTTP step endpoint).

Verifies:
  - delete() returns immediately, marks the DB is_deleting/inactive, cancels its
    own pending link/recognition jobs, and queues exactly one HASHDB_DELETE job;
  - a soft-deleting DB is hidden from the index, the REST list, and matching;
  - driving ``delete_one_step`` to completion removes the database, its products,
    its known files, and the recognised_products that referenced them, unlinks
    the ExtractedFile rows that pointed at the deleted known files, and queues a
    HASHDB_LINK relink for every other active database;
  - the state machine's cursor strictly advances and resumes to completion under
    a tight (past) wall-clock deadline;
  - per-product deletion (a separate route) is unchanged.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_hashdb_delete -v
"""

import json
import os
import sys
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hashdb-delete-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class _HashdbDeleteBase(unittest.TestCase):

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

        from myapp.database import User, UserPermission
        user = User(username='deleter', password_hash='x',
                    permission=UserPermission.READ_WRITE, can_use_api=True)
        db.session.add(user)
        db.session.commit()
        self.uid = user.id
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.uid)
            sess['_fresh'] = True

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _drive_delete_step(self, db_id, max_steps=200, tight=False):
        """Drive the in-process reap (``delete_one_step``) to completion.

        With ``tight=True`` each call gets a wall-clock deadline already in the
        past, so the state machine yields after one chunk — exercising the
        strictly-advancing cursor and the resume-to-completion contract the
        taskrunner relies on.  Returns the final result dict.
        """
        from myapp.database import HashDatabase
        from myapp.services.hashdb_jobs import delete_one_step
        cursor = 0
        last = None
        for _ in range(max_steps):
            database = self.db.session.get(HashDatabase, db_id)
            deadline = time.monotonic() if tight else None
            last = delete_one_step(database, cursor, deadline=deadline)
            if last['done']:
                return last
            self.assertGreater(last['cursor'], cursor,
                               'delete cursor must strictly advance')
            cursor = last['cursor']
        self.fail('delete did not finish within max_steps')

    def _seed_linked_db(self, name='Arcarc Apps', md5='bb' * 16, n_files=1,
                        with_recognition=True):
        """Create a HashDatabase with a product, known file(s), and an extracted
        file linked to each, plus a recognised_products row.  Returns (db_id,
        [extracted_file_ids])."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            RecognisedProduct,
            StorageDirectory,
        )
        db = self.db

        hdb = HashDatabase(name=name, is_active=True,
                           enable_product_recognition=with_recognition)
        db.session.add(hdb)
        db.session.flush()
        prod = KnownProduct(database_id=hdb.id, title='!Foo')
        db.session.add(prod)
        db.session.flush()

        item = Item(name='coll-' + name)
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path=f'{name}.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                         filesystem=FilesystemType.DFS)
        db.session.add(part)
        db.session.flush()

        ef_ids = []
        for i in range(n_files):
            file_md5 = md5 if n_files == 1 else f'{i:032x}'
            kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                           filename=f'!RunImage{i}', md5=file_md5, file_size=123)
            db.session.add(kf)
            db.session.flush()
            ef = ExtractedFile(partition_id=part.id, path=f'!Foo/!RunImage{i}',
                               filename=f'!RunImage{i}', md5=file_md5, file_size=123,
                               is_directory=False, known_file_id=kf.id)
            db.session.add(ef)
            db.session.flush()
            ef_ids.append(ef.id)
        db.session.add(RecognisedProduct(partition_id=part.id, product_id=prod.id,
                                         folder_path='!Foo'))
        db.session.commit()
        return hdb.id, ef_ids


class TestHashdbDeleteRoute(_HashdbDeleteBase):

    def test_delete_soft_marks_and_queues_job(self):
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            HashDatabase,
        )
        db_id, _ = self._seed_linked_db()

        resp = self.client.post(f'/hashdb/{db_id}/delete', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))
        self.db.session.expire_all()

        # The row is NOT gone yet — it is soft-deleted and hidden from matching.
        hdb = self.db.session.get(HashDatabase, db_id)
        self.assertIsNotNone(hdb)
        self.assertTrue(hdb.is_deleting)
        self.assertFalse(hdb.is_active)

        # Exactly one HASHDB_DELETE reap job is queued for this DB.
        jobs = Analysis.query.filter_by(
            analysis_type=AnalysisType.HASHDB_DELETE, artefact_id=None,
            status=AnalysisStatus.PENDING).all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0].hints)['database_id'], db_id)

    def test_delete_is_idempotent_on_repeat(self):
        from myapp.database import Analysis, AnalysisType
        db_id, _ = self._seed_linked_db()

        self.client.post(f'/hashdb/{db_id}/delete')
        self.client.post(f'/hashdb/{db_id}/delete')
        self.db.session.expire_all()
        self.assertEqual(
            Analysis.query.filter_by(
                analysis_type=AnalysisType.HASHDB_DELETE).count(), 1)

    def test_delete_cancels_own_pending_link_recognition_jobs(self):
        from myapp.database import Analysis, AnalysisType, HashDatabase
        from myapp.services.hash_rescan import (
            queue_hashdb_link_job,
            queue_hashdb_recognition_job,
        )
        db = self.db
        victim = HashDatabase(name='Busy', is_active=True,
                              enable_product_recognition=True)
        other = HashDatabase(name='Bystander', is_active=True)
        db.session.add_all([victim, other])
        db.session.flush()
        victim_id, other_id = victim.id, other.id
        queue_hashdb_link_job(victim_id)
        queue_hashdb_recognition_job(victim_id)
        queue_hashdb_link_job(other_id)
        db.session.commit()

        resp = self.client.post(f'/hashdb/{victim_id}/delete')
        self.assertIn(resp.status_code, (301, 302))
        db.session.expire_all()

        # The victim's link/recognition backfills are cancelled (only its
        # HASHDB_DELETE job remains); the bystander's link job survives.
        self.assertEqual(
            Analysis.query.filter(
                Analysis.analysis_type.in_(
                    [AnalysisType.HASHDB_LINK, AnalysisType.HASHDB_RECOGNITION]),
                Analysis.hints.like(f'%"database_id": {victim_id}%')).count(),
            0)
        self.assertEqual(
            Analysis.query.filter_by(
                analysis_type=AnalysisType.HASHDB_LINK).filter(
                Analysis.hints.like(f'%"database_id": {other_id}%')).count(),
            1)


class TestHashdbDeleteStep(_HashdbDeleteBase):

    def test_step_completes_cascade_and_unlinks(self):
        from myapp.database import (
            Analysis,
            AnalysisType,
            ExtractedFile,
            HashDatabase,
            KnownFile,
            KnownProduct,
            RecognisedProduct,
        )
        from myapp.database import HashDatabase as HDB
        db = self.db

        db_id, ef_ids = self._seed_linked_db(n_files=4, with_recognition=False)
        # A second active DB so we can assert a relink job is queued for it.
        other = HDB(name='Other', is_active=True)
        db.session.add(other)
        db.session.commit()
        other_id = other.id

        # Soft-delete, then drive the reap job to completion.
        self.client.post(f'/hashdb/{db_id}/delete')
        final = self._drive_delete_step(db_id)
        db.session.expire_all()

        self.assertTrue(final['done'])
        self.assertIsNone(db.session.get(HashDatabase, db_id))
        self.assertEqual(
            KnownFile.query.filter_by(database_id=db_id).count(), 0)
        self.assertEqual(
            KnownProduct.query.filter_by(database_id=db_id).count(), 0)
        self.assertEqual(RecognisedProduct.query.count(), 0)

        for ef_id in ef_ids:
            ef = db.session.get(ExtractedFile, ef_id)
            self.assertIsNotNone(ef)
            self.assertIsNone(ef.known_file_id)
            self.assertFalse(ef.is_known)

        # A relink job is queued for the other active DB (so freed files re-match).
        link_jobs = Analysis.query.filter_by(
            analysis_type=AnalysisType.HASHDB_LINK, artefact_id=None).all()
        linked = {json.loads(a.hints)['database_id'] for a in link_jobs}
        self.assertEqual(linked, {other_id})

    def test_step_cursor_advances_under_tight_deadline(self):
        from myapp.database import HashDatabase
        from myapp.services.hashdb_jobs import delete_one_step
        db_id, _ = self._seed_linked_db(n_files=6, with_recognition=False)
        self.client.post(f'/hashdb/{db_id}/delete')
        # A deadline already in the past returns done=False with an advanced
        # cursor after the first chunk.
        database = self.db.session.get(HashDatabase, db_id)
        result = delete_one_step(database, 0, deadline=time.monotonic())
        self.assertFalse(result['done'])
        self.assertGreater(result['cursor'], 0)

    def test_zero_deadline_still_makes_progress_to_completion(self):
        # Regression: a past/zero deadline must not make the step return
        # done=False with zero work forever (the advancing cursor would hide the
        # stall).  Each call does at least one chunk, so driving terminates.
        from myapp.database import HashDatabase
        db_id, _ = self._seed_linked_db(n_files=6, with_recognition=False)
        self.client.post(f'/hashdb/{db_id}/delete')
        final = self._drive_delete_step(db_id, tight=True)
        self.assertTrue(final['done'])
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(HashDatabase, db_id))


class TestHashdbDeleteHidesAndExcludes(_HashdbDeleteBase):

    def test_soft_deleting_hidden_from_index(self):
        from flask import template_rendered
        db_id, _ = self._seed_linked_db(name='Visible')
        self.client.post(f'/hashdb/{db_id}/delete')

        captured = []
        template_rendered.connect(
            lambda sender, template, context, **k: captured.append(context),
            self.app, weak=False)
        r = self.client.get('/hashdb/')
        self.assertEqual(r.status_code, 200, r.data)
        listed = {d.id for d in captured[-1]['databases']}
        self.assertNotIn(db_id, listed)

    def test_soft_deleting_hidden_from_api_list(self):
        db_id, _ = self._seed_linked_db(name='ApiVisible')
        self.client.post(f'/hashdb/{db_id}/delete')
        resp = self.client.get('/api/hash-databases',
                               headers={'X-API-Key': _WORKER_KEY})
        self.assertEqual(resp.status_code, 200, resp.data)
        ids = {row['id'] for row in resp.get_json()}
        self.assertNotIn(db_id, ids)

    def test_soft_deleting_route_is_404(self):
        db_id, _ = self._seed_linked_db(name='Gone')
        self.client.post(f'/hashdb/{db_id}/delete')
        self.assertEqual(self.client.get(f'/hashdb/{db_id}').status_code, 404)

    def test_soft_deleting_excluded_from_matching(self):
        # is_active=False (set alongside is_deleting) drops the DB out of the
        # active-known-file matching query for free.
        from myapp.services.hash_rescan import find_known_file
        db_id, _ = self._seed_linked_db(name='NoMatch', md5='ee' * 16)
        self.assertIsNotNone(find_known_file(md5='ee' * 16))  # matches before
        self.client.post(f'/hashdb/{db_id}/delete')
        self.db.session.expire_all()
        self.assertIsNone(find_known_file(md5='ee' * 16))      # excluded after


class TestHashdbProductDeleteUnchanged(_HashdbDeleteBase):

    def test_delete_product_removes_recognitions_and_unlinks(self):
        # The per-product delete route is independent of the async DB delete and
        # still removes a product's recognitions / known files inline.
        from arcology_shared.enums import ArtefactType
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            RecognisedProduct,
            StorageDirectory,
        )
        db = self.db
        hdb = HashDatabase(name='Apps', is_active=True,
                           enable_product_recognition=False)
        db.session.add(hdb)
        db.session.flush()
        prod = KnownProduct(database_id=hdb.id, title='!System')
        db.session.add(prod)
        db.session.flush()
        kf = KnownFile(database_id=hdb.id, product_id=prod.id,
                       filename='Modules', md5='cc' * 16, file_size=42)
        db.session.add(kf)
        db.session.flush()

        item = Item(name='coll')
        db.session.add(item)
        db.session.flush()
        art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                       original_filename='d.ssd', storage_path='d.ssd',
                       storage_directory=StorageDirectory.UPLOADS)
        db.session.add(art)
        db.session.flush()

        ef_ids = []
        for i in range(5):
            part = Partition(artefact_id=art.id, partition_index=i, label=f'P{i}',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            db.session.add(RecognisedProduct(partition_id=part.id,
                                             product_id=prod.id,
                                             folder_path='!System'))
            ef = ExtractedFile(partition_id=part.id, path='!System/Modules',
                               filename='Modules', md5='cc' * 16, file_size=42,
                               is_directory=False,
                               known_file_id=kf.id)
            db.session.add(ef)
            db.session.flush()
            ef_ids.append(ef.id)
        db_id, pid = hdb.id, prod.id
        db.session.commit()

        resp = self.client.post(f'/hashdb/{db_id}/products/{pid}/delete',
                                follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))
        db.session.expire_all()

        self.assertIsNone(db.session.get(KnownProduct, pid))
        self.assertEqual(
            KnownFile.query.filter_by(product_id=pid).count(), 0)
        self.assertEqual(
            RecognisedProduct.query.filter_by(product_id=pid).count(), 0)
        self.assertIsNotNone(db.session.get(HashDatabase, db_id))
        for ef_id in ef_ids:
            ef = db.session.get(ExtractedFile, ef_id)
            self.assertIsNotNone(ef)
            self.assertIsNone(ef.known_file_id)
            self.assertFalse(ef.is_known)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
