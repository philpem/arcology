"""
Tests for the `arco hashdb import` client driver (cmd_hashdb_import).

Exercises the batched import path against a fake client (no real HTTP):
  - a JSON import sends products via the batch endpoint;
  - a 404 from the batch endpoint falls back to the per-product endpoints.

Run:
    python -m unittest ci.test_cli_hashdb_import -v
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI_ROOT = os.path.join(_REPO_ROOT, 'cli')
for _p in (_REPO_ROOT, _CLI_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arccli.client import ArcologyError  # noqa: E402
from arccli.commands.hashdb import cmd_hashdb_import  # noqa: E402

_DOC = {
    'database': {'name': 'CLI DB'},
    'products': [
        {'title': '!Foo', 'files': [{'filename': '!RunImage', 'md5': 'aa' * 16}]},
        {'title': '!Bar', 'files': [{'filename': 'data', 'sha1': 'bb' * 20}]},
    ],
}


class _BatchClient:
    """Fake client whose batch endpoint succeeds."""

    def __init__(self):
        self.created_dbs = []
        self.batch_calls = []
        self.link_calls = []
        self.per_product_calls = []

    def list_hash_databases(self):
        return []

    def create_hash_database(self, **data):
        self.created_dbs.append(data)
        return {'id': 7}

    def import_hash_database_products(self, db_id, products, link=True):
        self.batch_calls.append((db_id, products, link))
        return {'products': len(products),
                'files': sum(len(p.get('files', [])) for p in products)}

    def queue_hash_database_link(self, db_id):
        self.link_calls.append(db_id)
        return {'status': 'queued'}

    # Per-product endpoints should NOT be hit on the batch path.
    def create_hash_database_product(self, db_id, **data):
        self.per_product_calls.append(('product', db_id, data))
        return {'id': 99}

    def add_product_files(self, db_id, product_id, files):
        self.per_product_calls.append(('files', db_id, product_id, files))
        return {'added': len(files)}


class _FallbackClient(_BatchClient):
    """Fake client whose batch endpoint 404s (old server)."""

    def import_hash_database_products(self, db_id, products, link=True):
        raise ArcologyError('Not found.', 404)


def _args(input_file):
    return SimpleNamespace(input_file=input_file, format='json',
                           name=None, merge=False, json=False)


class TestCliHashdbImport(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as fh:
            json.dump(_DOC, fh)

    def tearDown(self):
        os.unlink(self.path)

    def test_batch_path(self):
        client = _BatchClient()
        cmd_hashdb_import(client, _args(self.path))
        self.assertEqual(len(client.batch_calls), 1)
        db_id, products, link = client.batch_calls[0]
        self.assertEqual(db_id, 7)
        self.assertEqual(len(products), 2)
        self.assertFalse(link)
        self.assertEqual(client.link_calls, [7])
        self.assertEqual(client.per_product_calls, [])  # batch path only

    def test_fallback_on_404(self):
        client = _FallbackClient()
        cmd_hashdb_import(client, _args(self.path))
        # Falls back to 2 products + 2 file-adds via the per-product endpoints.
        kinds = [c[0] for c in client.per_product_calls]
        self.assertEqual(kinds.count('product'), 2)
        self.assertEqual(kinds.count('files'), 2)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
