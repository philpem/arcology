"""
Tests for the arco CLI chunked-upload client
(``cli/arccli/client.py``: async finalise + resume).

Drives ``ArcologyClient.upload_artefact_chunked`` against an in-memory fake of
the chunked-upload protocol (no real HTTP, no real server), exercising:
  - the async /complete + /complete/status poll loop;
  - resume that skips chunks the server already holds;
  - patient per-chunk retry across a transient connection error.

The resume sidecar is redirected to a temp dir so the real ~/.config is never
touched.

Run:
    python -m unittest ci.test_cli_chunked -v
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI_ROOT = os.path.join(_REPO_ROOT, 'cli')
for _p in (_REPO_ROOT, _CLI_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _FakeResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = str(self._json)

    def json(self):
        return self._json


class _FakeServer:
    """Minimal in-memory implementation of the chunked-upload protocol."""

    def __init__(self):
        self.sessions = {}
        self._next = 0
        self.chunk_posts = []          # (uuid, index) for every accepted chunk
        self.fail_once = set()         # (uuid, index) to fail once with a net error

    def _new_uuid(self):
        self._next += 1
        return f'{self._next:032x}'

    # Wired into the client's requests.Session ---------------------------
    def get(self, url, **kw):
        return self._dispatch('GET', url, kw)

    def post(self, url, **kw):
        return self._dispatch('POST', url, kw)

    def _dispatch(self, method, url, kw):
        path = url.split('/api/', 1)[1]

        if path == 'uploads/chunked/init' and method == 'POST':
            uuid_ = self._new_uuid()
            self.sessions[uuid_] = {
                'total_chunks': kw['json']['total_chunks'],
                'received': set(),
                'state': 'pending',
                'artefact': None,
            }
            return _FakeResponse(201, {'upload_uuid': uuid_})

        m = re.match(r'uploads/chunked/([0-9a-f]+)/chunk/(\d+)$', path)
        if m and method == 'POST':
            uuid_, idx = m.group(1), int(m.group(2))
            sess = self.sessions.get(uuid_)
            if sess is None:
                return _FakeResponse(404, {'error': 'not found'})
            if sess['state'] != 'pending':
                return _FakeResponse(409, {'error': 'being finalised'})
            if (uuid_, idx) in self.fail_once:
                self.fail_once.discard((uuid_, idx))
                raise requests.exceptions.ConnectionError('transient')
            sess['received'].add(idx)
            self.chunk_posts.append((uuid_, idx))
            return _FakeResponse(200, {'received': True, 'chunk': idx})

        m = re.match(r'uploads/chunked/([0-9a-f]+)/complete/status$', path)
        if m and method == 'GET':
            return self._complete_status(m.group(1))

        m = re.match(r'uploads/chunked/([0-9a-f]+)/complete$', path)
        if m and method == 'POST':
            return self._complete(m.group(1))

        m = re.match(r'uploads/chunked/([0-9a-f]+)/status$', path)
        if m and method == 'GET':
            sess = self.sessions.get(m.group(1))
            if sess is None:
                return _FakeResponse(404, {'error': 'not found'})
            return _FakeResponse(200, {
                'total_chunks': sess['total_chunks'],
                'received_chunks': sorted(sess['received']),
            })

        return _FakeResponse(404, {'error': f'unhandled {method} {path}'})

    def _complete(self, uuid_):
        sess = self.sessions.get(uuid_)
        if sess is None:
            return _FakeResponse(404, {'error': 'not found'})
        if sess['state'] == 'pending':
            sess['state'] = 'assembling'  # finalise begins; client must poll
        return _FakeResponse(202, {'upload_uuid': uuid_, 'state': sess['state']})

    def _complete_status(self, uuid_):
        sess = self.sessions.get(uuid_)
        if sess is None:
            return _FakeResponse(404, {'error': 'not found'})
        if sess['state'] == 'assembling':
            # Simulate the assembly finishing between polls.
            sess['state'] = 'done'
            sess['artefact'] = {
                'uuid': uuid_, 'artefact_type': 'raw_sector',
                'md5': 'm', 'sha256': 's', 'file_size': 10,
                'original_filename': 'big.img',
            }
        if sess['state'] == 'done':
            return _FakeResponse(200, {'state': 'done', 'artefact': sess['artefact']})
        if sess['state'] == 'failed':
            return _FakeResponse(200, {'state': 'failed', 'error': 'boom',
                                       'error_code': 'internal'})
        return _FakeResponse(202, {'upload_uuid': uuid_, 'state': sess['state']})


class TestCLIChunkedClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from arccli import client as _client
        cls.client_mod = _client

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='arcology-ci-cliresume-')
        # Redirect the resume sidecar away from the real ~/.config.
        self._orig_store = self.client_mod.RESUME_STORE
        self._orig_dir = self.client_mod.CONFIG_DIR
        self.client_mod.CONFIG_DIR = Path(self._tmp)
        self.client_mod.RESUME_STORE = Path(self._tmp) / 'resume.json'

        self.server = _FakeServer()
        self.client = self.client_mod.ArcologyClient('http://test', 'key')
        self.client.session = self.server  # swap in the fake transport

        # A small file uploaded with a tiny chunk size to force several chunks.
        self.filepath = os.path.join(self._tmp, 'big.img')
        with open(self.filepath, 'wb') as f:
            f.write(b'0123456789')  # 10 bytes
        self.chunk_size = 4  # -> 3 chunks (4 + 4 + 2)

    def tearDown(self):
        self.client_mod.RESUME_STORE = self._orig_store
        self.client_mod.CONFIG_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _upload(self, **kw):
        return self.client.upload_artefact_chunked(
            'item-uuid', self.filepath, 'Label', chunk_size=self.chunk_size, **kw)

    def test_async_happy_path(self):
        art = self._upload()
        self.assertEqual(art['original_filename'], 'big.img')
        # All three chunks uploaded exactly once.
        self.assertEqual(sorted(i for _, i in self.server.chunk_posts), [0, 1, 2])
        # Resume sidecar entry cleared on success.
        key = self.client._resume_key(self.filepath, 'item-uuid', 3)
        self.assertIsNone(self.client._load_resume(key))

    def test_retry_rides_out_transient_error(self):
        # Force chunk 1's first POST to raise a connection error.
        # The uuid isn't known yet, so fail by index across any session.
        orig_dispatch = self.server._dispatch
        state = {'failed': False}

        def flaky(method, url, kw):
            m = re.match(r'.*/chunk/(\d+)$', url)
            if m and int(m.group(1)) == 1 and not state['failed']:
                state['failed'] = True
                raise requests.exceptions.ConnectionError('transient')
            return orig_dispatch(method, url, kw)

        self.server._dispatch = flaky
        # Speed up the retry backoff.
        self.client_mod.CHUNK_RETRY_MAX_BACKOFF = 0
        art = self._upload()
        self.assertEqual(art['original_filename'], 'big.img')
        self.assertTrue(state['failed'])
        self.assertEqual(sorted(i for _, i in self.server.chunk_posts), [0, 1, 2])

    def test_resume_skips_received_chunks(self):
        # Simulate a prior interrupted run: a session with chunks 0 and 2 in.
        uuid_ = self.server._new_uuid()
        self.server.sessions[uuid_] = {
            'total_chunks': 3, 'received': {0, 2}, 'state': 'pending',
            'artefact': None}
        key = self.client._resume_key(self.filepath, 'item-uuid', 3)
        self.client._save_resume(key, uuid_)

        art = self._upload()
        self.assertEqual(art['uuid'], uuid_)
        # Only the missing chunk (1) was uploaded on resume.
        self.assertEqual([i for _, i in self.server.chunk_posts], [1])

    def test_resume_done_returns_existing_artefact(self):
        # A prior run that already finished finalise but didn't clear resume.
        uuid_ = self.server._new_uuid()
        self.server.sessions[uuid_] = {
            'total_chunks': 3, 'received': {0, 1, 2}, 'state': 'done',
            'artefact': {'uuid': uuid_, 'artefact_type': 'raw_sector',
                         'md5': 'm', 'sha256': 's', 'file_size': 10,
                         'original_filename': 'big.img'}}
        key = self.client._resume_key(self.filepath, 'item-uuid', 3)
        self.client._save_resume(key, uuid_)

        art = self._upload()
        self.assertEqual(art['uuid'], uuid_)
        # Nothing re-uploaded.
        self.assertEqual(self.server.chunk_posts, [])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
