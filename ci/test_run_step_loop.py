"""
Unit tests for the worker bounded-step driver ``run_step_loop``.

Focus on the adaptive batch-size behaviour added for recognition steps: a
server-signalled ``timed_out`` result must retry the *same* cursor with a
halved batch (down to ``min_limit``) rather than failing the whole job, and a
batch that cannot be subdivided further must fail cleanly instead of looping.

Run:
    python -m unittest ci.test_run_step_loop -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.analyses._common import run_step_loop  # noqa: E402


class TestRunStepLoop(unittest.TestCase):

    def test_legacy_single_arg_step_unchanged(self):
        """Without initial_limit the step is still called as step(cursor)."""
        calls = []

        def step(cursor):
            calls.append(cursor)
            if cursor == 0:
                return {'done': False, 'next_id': 5, 'processed': 5}
            return {'done': True, 'next_id': 5, 'processed': 0}

        result, totals = run_step_loop(step, cursor_key='next_id')
        self.assertTrue(result['done'])
        self.assertEqual(totals['processed'], 5)
        self.assertEqual(calls, [0, 5])

    def test_timed_out_without_limit_fails(self):
        """A timeout signal with no adaptive limit is unrecoverable."""
        result, _ = run_step_loop(
            lambda cursor: {'timed_out': True, 'next_id': cursor},
            cursor_key='next_id')
        self.assertIsNone(result)

    def test_timed_out_halves_limit_and_makes_progress(self):
        """Timeouts halve the batch at the same cursor until it succeeds."""
        seen_limits = []

        def step(cursor, limit):
            seen_limits.append((cursor, limit))
            # Times out until the batch is small enough (<= 6), then completes.
            if limit > 6:
                return {'timed_out': True, 'next_product_id': cursor}
            return {'done': True, 'next_product_id': cursor, 'processed': limit,
                    'matches': 2}

        result, totals = run_step_loop(
            step, cursor_key='next_product_id', initial_limit=25)
        self.assertTrue(result['done'])
        # 25 -> 12 -> 6 (success), all at cursor 0.
        self.assertEqual(seen_limits, [(0, 25), (0, 12), (0, 6)])
        self.assertEqual(totals['processed'], 6)
        self.assertEqual(totals['matches'], 2)

    def test_limit_ramps_back_up_after_a_slow_patch(self):
        """After halving, a successful step doubles the batch back toward initial."""
        seen = []

        def step(cursor, limit):
            seen.append((cursor, limit))
            if cursor == 0 and limit > 6:
                return {'timed_out': True, 'next_product_id': 0}
            # Each success advances the cursor by one "unit" until cursor 4.
            if cursor < 4:
                return {'done': False, 'next_product_id': cursor + 1, 'processed': limit}
            return {'done': True, 'next_product_id': cursor, 'processed': 0}

        result, _ = run_step_loop(
            step, cursor_key='next_product_id', initial_limit=25)
        self.assertTrue(result['done'])
        # 25->12->6 (success at 6), then doubles 6->12->24->25 (capped) on success.
        self.assertEqual(
            seen,
            [(0, 25), (0, 12), (0, 6), (1, 12), (2, 24), (3, 25), (4, 25)])

    def test_floor_skip_result_continues_and_counts_skipped(self):
        """At the floor the server advances via a normal (non-timed_out) skip
        result; the loop continues and accumulates the skipped count."""
        seen = []

        def step(cursor, limit):
            seen.append((cursor, limit))
            if cursor == 0 and limit > 1:
                return {'timed_out': True, 'next_product_id': 0}
            if cursor == 0:  # limit == 1: server skips the stuck product
                return {'done': False, 'next_product_id': 5, 'processed': 0,
                        'matches': 0, 'skipped': 1}
            return {'done': True, 'next_product_id': 5, 'processed': 0}

        result, totals = run_step_loop(
            step, cursor_key='next_product_id', initial_limit=4, min_limit=1)
        self.assertTrue(result['done'])
        self.assertEqual(totals['skipped'], 1)
        # 4->2->1 (skip, advance to 5), then a final step that reports done.
        self.assertEqual(seen, [(0, 4), (0, 2), (0, 1), (5, 2)])

    def test_progress_label_and_total_relayed_to_reporter(self):
        """Server-supplied progress_label/progress_total update the reporter and
        are not summed into totals."""

        class _Reporter:
            label = 'Processing'
            total = None

            def __init__(self):
                self.updates = []

            def update(self, done=None, **k):
                self.updates.append((self.label, self.total, done))
                return True

        reporter = _Reporter()

        def step(cursor, limit):
            if cursor == 0:
                return {'done': False, 'next_id': 1, 'processed': 2,
                        'progress_label': "Linking files in HashDB 'X'",
                        'progress_total': 3456}
            return {'done': True, 'next_id': 1, 'processed': 0}

        result, totals = run_step_loop(
            step, cursor_key='next_id', reporter=reporter, initial_limit=25)
        self.assertTrue(result['done'])
        self.assertEqual(reporter.label, "Linking files in HashDB 'X'")
        self.assertEqual(reporter.total, 3456)
        # progress_total must NOT be summed into totals.
        self.assertNotIn('progress_total', totals)
        self.assertEqual(totals['processed'], 2)

    def test_timeout_at_min_limit_fails(self):
        """A batch that still times out at min_limit fails the loop."""
        calls = []

        def step(cursor, limit):
            calls.append(limit)
            return {'timed_out': True, 'next_product_id': cursor}

        result, _ = run_step_loop(
            step, cursor_key='next_product_id', initial_limit=4, min_limit=1)
        self.assertIsNone(result)
        # 4 -> 2 -> 1 -> fail (does not retry below min_limit).
        self.assertEqual(calls, [4, 2, 1])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
