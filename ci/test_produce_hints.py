"""
Tests for _merge_produce_hints() in myapp.blueprints.api.

Derived artefacts inherit the parent analysis's hints, but the independent-sides
split needs to tag each side with a per-artefact hint (partition_index_base).
_merge_produce_hints overlays the produce-artefact payload's hints on top of the
parent analysis's hints.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_produce_hints -v
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-produce-hints-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _analysis(hints):
    return SimpleNamespace(hints=json.dumps(hints) if hints is not None else None)


class TestMergeProduceHints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.blueprints.api import _merge_produce_hints
        cls.merge = staticmethod(_merge_produce_hints)

    def test_no_hints_returns_none(self):
        self.assertIsNone(self.merge(_analysis(None), {}))

    def test_payload_only(self):
        result = self.merge(_analysis(None), {'hints': {'partition_index_base': 1}})
        self.assertEqual(result, {'partition_index_base': 1})

    def test_parent_only(self):
        result = self.merge(_analysis({'filesystem': 'dfs'}), {})
        self.assertEqual(result, {'filesystem': 'dfs'})

    def test_merge_parent_and_payload(self):
        result = self.merge(
            _analysis({'filesystem': 'dfs'}),
            {'hints': {'partition_index_base': 1}},
        )
        self.assertEqual(result, {'filesystem': 'dfs', 'partition_index_base': 1})

    def test_payload_overrides_parent_on_conflict(self):
        result = self.merge(
            _analysis({'partition_index_base': 0}),
            {'hints': {'partition_index_base': 1}},
        )
        self.assertEqual(result['partition_index_base'], 1)

    def test_non_dict_payload_hints_ignored(self):
        result = self.merge(_analysis({'filesystem': 'dfs'}), {'hints': 'nope'})
        self.assertEqual(result, {'filesystem': 'dfs'})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
