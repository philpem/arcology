"""
Tests for the asynchronous chunked-upload finalise core
(``myapp/services/chunked_upload.py``).

Exercises the service layer directly (no HTTP): the meta.json finalise state
machine, the atomic single-winner claim, stale re-drive, the finalise runner's
success/failure outcomes, and — the key correctness property — that two claim
attempts never both run a finalise (which would create duplicate artefacts).

Uses SQLite in-memory + LocalStorage (temp dirs).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_chunked_finalize -v
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-chunked-finalize-secret-key-not-for-prod')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestChunkedFinalizeCore(unittest.TestCase):
    """Service-level tests for the async finalise state machine and runner."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls._tmpdir = tempfile.mkdtemp(prefix='arcology-ci-finalize-')
        upload_dir = os.path.join(cls._tmpdir, 'uploads')
        output_dir = os.path.join(cls._tmpdir, 'outputs')
        os.makedirs(upload_dir)
        os.makedirs(output_dir)

        cls.app = create_app()
        cls.app.config.update({
            'TESTING': True,
            'UPLOAD_FOLDER': upload_dir,
            'OUTPUT_FOLDER': output_dir,
        })
        from arcology_shared.storage import create_storage
        storage_cfg = dict(cls.app.config)
        storage_cfg['UPLOAD_FOLDER'] = upload_dir
        storage_cfg['OUTPUT_FOLDER'] = output_dir
        with cls.app.app_context():
            cls.app.storage = create_storage(storage_cfg)

        cls.db = _db
        with cls.app.app_context():
            _db.create_all()
            cls.item_pk, cls.item_uuid = cls._seed(_db)

    @classmethod
    def tearDownClass(cls):
        from myapp.services import chunked_upload as _chunked
        _chunked.shutdown_executor(wait=True)
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    @classmethod
    def _seed(cls, db):
        from myapp.database import Item, Platform
        platform = Platform(name='Finalize Test Platform')
        db.session.add(platform)
        db.session.flush()
        item = Item(name='Finalize Test Item', platform_id=platform.id)
        db.session.add(item)
        db.session.commit()
        return item.id, item.uuid

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_session(self, *, label, filename='fin.img', chunks=(b'AAAA', b'BBBB')):
        """Create a chunk session on disk with the given chunk data, return its uuid."""
        from myapp.services import chunked_upload as _chunked
        meta = {
            'filename': filename,
            'total_chunks': len(chunks),
            'item_uuid': self.item_uuid,
            'item_id': self.item_pk,
            'label': label,
            'auto_analyse': False,
        }
        upload_uuid = _chunked.init_chunk_session(meta)
        for i, c in enumerate(chunks):
            _chunked.write_chunk(upload_uuid, i, c)
        return upload_uuid

    def _make_finalize_fn(self, *, label, filename='fin.img', counter=None):
        """Build an ingest closure mirroring what the blueprints supply."""
        from arcology_shared.artefact_types import detect_artefact_type
        from myapp.database import Item
        from myapp.extensions import db
        from myapp.services.upload_pipeline import QUEUE_NONE, ingest_uploaded_artefact
        item_id = self.item_pk

        def fn(assembled):
            if counter is not None:
                counter.append(1)
            item = db.session.get(Item, item_id)
            outcome = ingest_uploaded_artefact(
                item, label=label,
                artefact_type=detect_artefact_type(filename),
                type_overridden=False, original_filename=filename,
                storage_name=assembled.storage_name,
                file_size=assembled.file_size,
                md5=assembled.md5, sha256=assembled.sha256,
                queue=QUEUE_NONE)
            return outcome.artefact.uuid
        return fn

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def test_fresh_session_is_pending(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='Pending')
            status = _chunked.finalize_status(uuid_)
            self.assertEqual(status, {'state': 'pending'})

    def test_status_none_for_unknown(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            self.assertIsNone(_chunked.finalize_status('a' * 32))

    def test_claim_then_run_produces_done_and_one_artefact(self):
        from myapp.database import Artefact
        from myapp.extensions import db
        from myapp.services import chunked_upload as _chunked
        calls = []
        with self.app.app_context():
            uuid_ = self._make_session(label='RunOnce')
            self.assertTrue(_chunked.claim_finalize(uuid_))
            _chunked.run_finalize(
                uuid_, self._make_finalize_fn(label='RunOnce', counter=calls))

            status = _chunked.finalize_status(uuid_)
            self.assertEqual(status['state'], 'done')
            art_uuid = status['artefact_uuid']
            self.assertRegex(art_uuid, r'^[0-9a-f]{32}$')

            arts = db.session.scalars(
                db.select(Artefact).filter_by(label='RunOnce')).all()
            self.assertEqual(len(arts), 1)
            self.assertEqual(arts[0].uuid, art_uuid)
            self.assertEqual(len(calls), 1)

            # Chunk files removed, meta retained for status polling.
            cdir = _chunked.chunk_dir(uuid_)
            self.assertTrue(os.path.exists(os.path.join(cdir, 'meta.json')))
            self.assertFalse(os.path.exists(os.path.join(cdir, '000000')))

    def test_double_claim_yields_single_winner(self):
        """Two claims on a fresh/live session: exactly one wins (no double ingest)."""
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='SingleWinner')
            first = _chunked.claim_finalize(uuid_)
            second = _chunked.claim_finalize(uuid_)  # now 'assembling', fresh heartbeat
            self.assertTrue(first)
            self.assertFalse(second)

    def test_done_session_not_reclaimable(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='DoneLock')
            self.assertTrue(_chunked.claim_finalize(uuid_))
            _chunked.run_finalize(uuid_, self._make_finalize_fn(label='DoneLock'))
            self.assertEqual(_chunked.finalize_status(uuid_)['state'], 'done')
            # A re-drive attempt on a done session must not re-run.
            self.assertFalse(_chunked.claim_finalize(uuid_))

    def test_stale_assembling_is_reclaimable(self):
        """An 'assembling' session whose heartbeat has aged out can be re-driven."""
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='Stale')
            self.assertTrue(_chunked.claim_finalize(uuid_))
            # Back-date the heartbeat well past the stale threshold.
            cdir = _chunked.chunk_dir(uuid_)
            meta = _chunked._read_meta_dir(cdir)
            meta['heartbeat_at'] = time.time() - (_chunked.finalize_stale_seconds() + 60)
            _chunked._write_meta_dir(cdir, meta)

            self.assertTrue(_chunked.finalize_is_stale(uuid_))
            self.assertTrue(_chunked.claim_finalize(uuid_))  # re-drive wins
            self.assertEqual(_chunked.read_meta(uuid_)['attempts'], 2)

    def test_live_assembling_not_stale(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='Live')
            self.assertTrue(_chunked.claim_finalize(uuid_))
            self.assertFalse(_chunked.finalize_is_stale(uuid_))

    # ------------------------------------------------------------------
    # Failure outcomes
    # ------------------------------------------------------------------

    def test_oversize_records_failed_too_large(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            self.app.config['MAX_UPLOAD_SIZE'] = 4
            try:
                uuid_ = self._make_session(label='TooBig',
                                           chunks=(b'way too many bytes',))
                self.assertTrue(_chunked.claim_finalize(uuid_))
                _chunked.run_finalize(uuid_, self._make_finalize_fn(label='TooBig'))
                status = _chunked.finalize_status(uuid_)
                self.assertEqual(status['state'], 'failed')
                self.assertEqual(status['error_code'], 'too_large')
                # Chunk files must be dropped on failure (only meta is retained),
                # not left pinning disk until the result-TTL purge.
                cdir = _chunked.chunk_dir(uuid_)
                self.assertTrue(os.path.exists(os.path.join(cdir, 'meta.json')))
                self.assertFalse(os.path.exists(os.path.join(cdir, '000000')))
            finally:
                self.app.config.pop('MAX_UPLOAD_SIZE', None)

    def test_internal_error_records_failed(self):
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='Boom')
            self.assertTrue(_chunked.claim_finalize(uuid_))

            def boom(assembled):
                raise RuntimeError('kaboom')

            _chunked.run_finalize(uuid_, boom)
            status = _chunked.finalize_status(uuid_)
            self.assertEqual(status['state'], 'failed')
            self.assertEqual(status['error_code'], 'internal')

    # ------------------------------------------------------------------
    # Pool submission
    # ------------------------------------------------------------------

    def test_submit_finalize_runs_via_pool(self):
        from myapp.database import Artefact
        from myapp.extensions import db
        from myapp.services import chunked_upload as _chunked
        with self.app.app_context():
            uuid_ = self._make_session(label='ViaPool')
            self.assertTrue(_chunked.claim_finalize(uuid_))
            _chunked.submit_finalize(
                uuid_, self._make_finalize_fn(label='ViaPool'))

            # Poll for completion (the pool runs the job on another thread).
            deadline = time.time() + 10
            while time.time() < deadline:
                if _chunked.finalize_status(uuid_)['state'] == 'done':
                    break
                time.sleep(0.05)
            self.assertEqual(_chunked.finalize_status(uuid_)['state'], 'done')
            self.assertEqual(
                len(db.session.scalars(
                    db.select(Artefact).filter_by(label='ViaPool')).all()), 1)


class TestChunkedFinalizeAPIEndpoints(unittest.TestCase):
    """HTTP-level tests for the async /complete + /complete/status API routes."""

    @classmethod
    def setUpClass(cls):
        import json
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls._json = json
        cls._tmpdir = tempfile.mkdtemp(prefix='arcology-ci-finalize-api-')
        upload_dir = os.path.join(cls._tmpdir, 'uploads')
        output_dir = os.path.join(cls._tmpdir, 'outputs')
        os.makedirs(upload_dir)
        os.makedirs(output_dir)

        cls.app = create_app()
        cls.app.config.update({
            'TESTING': True,
            'UPLOAD_FOLDER': upload_dir,
            'OUTPUT_FOLDER': output_dir,
        })
        from arcology_shared.storage import create_storage
        storage_cfg = dict(cls.app.config)
        storage_cfg['UPLOAD_FOLDER'] = upload_dir
        storage_cfg['OUTPUT_FOLDER'] = output_dir
        with cls.app.app_context():
            cls.app.storage = create_storage(storage_cfg)

        cls.client = cls.app.test_client()
        with cls.app.app_context():
            _db.create_all()
            from myapp.database import Item, Platform
            platform = Platform(name='API Finalize Platform')
            _db.session.add(platform)
            _db.session.flush()
            item = Item(name='API Finalize Item', platform_id=platform.id)
            _db.session.add(item)
            _db.session.commit()
            cls.item_uuid = item.uuid

    @classmethod
    def tearDownClass(cls):
        from myapp.services import chunked_upload as _chunked
        _chunked.shutdown_executor(wait=True)
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    _AUTH = {'X-API-Key': os.environ['WORKER_API_KEY']}

    def _init(self, total_chunks=2, **extra):
        payload = {'filename': 'async.img', 'total_chunks': total_chunks,
                   'item_uuid': self.item_uuid, 'label': extra.pop('label', 'Async')}
        payload.update(extra)
        r = self.client.post('/api/uploads/chunked/init',
                             data=self._json.dumps(payload),
                             content_type='application/json', headers=self._AUTH)
        return r.get_json()['upload_uuid']

    def _chunk(self, uuid_, idx, data):
        return self.client.post(f'/api/uploads/chunked/{uuid_}/chunk/{idx}',
                                data=data, content_type='application/octet-stream',
                                headers=self._AUTH)

    def _complete_async(self, uuid_):
        return self.client.post(f'/api/uploads/chunked/{uuid_}/complete',
                                data=self._json.dumps({'async': True}),
                                content_type='application/json', headers=self._AUTH)

    def _poll(self, uuid_, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.get(
                f'/api/uploads/chunked/{uuid_}/complete/status', headers=self._AUTH)
            body = r.get_json()
            if body['state'] in ('done', 'failed'):
                return r.status_code, body
            time.sleep(0.05)
        self.fail('finalise did not complete within timeout')

    def test_async_complete_then_poll_done(self):
        uuid_ = self._init(total_chunks=2, label='AsyncDone')
        self._chunk(uuid_, 0, b'AAAA')
        self._chunk(uuid_, 1, b'BBBB')

        r = self._complete_async(uuid_)
        self.assertEqual(r.status_code, 202, r.data)
        self.assertIn(r.get_json()['state'], ('pending', 'assembling', 'done'))

        code, body = self._poll(uuid_)
        self.assertEqual(code, 200)
        self.assertEqual(body['state'], 'done')
        self.assertEqual(body['artefact']['original_filename'], 'async.img')
        self.assertEqual(body['artefact']['file_size'], 8)

    def test_chunk_rejected_after_finalise_started(self):
        uuid_ = self._init(total_chunks=2, label='LateChunk')
        self._chunk(uuid_, 0, b'AAAA')
        self._chunk(uuid_, 1, b'BBBB')
        self.assertEqual(self._complete_async(uuid_).status_code, 202)
        # A late chunk write must be refused (409) regardless of finalise state.
        self.assertEqual(self._chunk(uuid_, 0, b'XXXX').status_code, 409)
        self._poll(uuid_)  # drain to done so teardown is clean

    def test_stale_assembling_redriven_via_status(self):
        from myapp.services import chunked_upload as _chunked
        uuid_ = self._init(total_chunks=1, label='Redrive')
        self._chunk(uuid_, 0, b'data')
        # Simulate a finalise orphaned by a restart: claim it (-> assembling) but
        # never submit, then age the heartbeat past the stale threshold.
        with self.app.app_context():
            self.assertTrue(_chunked.claim_finalize(uuid_))
            cdir = _chunked.chunk_dir(uuid_)
            meta = _chunked._read_meta_dir(cdir)
            meta['heartbeat_at'] = time.time() - (_chunked.finalize_stale_seconds() + 60)
            _chunked._write_meta_dir(cdir, meta)
        # A status poll must re-authorise and re-drive it to completion.
        code, body = self._poll(uuid_)
        self.assertEqual(code, 200)
        self.assertEqual(body['state'], 'done')

    def test_status_404_for_unknown(self):
        r = self.client.get(
            f'/api/uploads/chunked/{"a" * 32}/complete/status', headers=self._AUTH)
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
