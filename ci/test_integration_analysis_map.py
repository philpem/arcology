"""Guard test: the integration harness's ANALYSIS_MAP copy must not drift.

This runs in the normal app-tests job (Flask installed), unlike the integration
suite itself which needs the worker container.  It asserts that
``IT_ANALYSIS_MAP`` (worker-importable, used by the fake server) agrees with the
real ``ANALYSIS_MAP`` for every artefact type it covers, so the fake server
queues exactly what the web app would.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'integration'))


class TestIntegrationAnalysisMap(unittest.TestCase):
    def test_it_map_matches_real_map(self):
        from harness.analysis_map import IT_ANALYSIS_MAP
        from myapp.services.artefact_types import ANALYSIS_MAP

        for artefact_type, expected in IT_ANALYSIS_MAP.items():
            with self.subTest(artefact_type=artefact_type):
                self.assertIn(
                    artefact_type, ANALYSIS_MAP,
                    f"{artefact_type} is in IT_ANALYSIS_MAP but missing from the "
                    f"real ANALYSIS_MAP",
                )
                self.assertEqual(
                    list(expected), list(ANALYSIS_MAP[artefact_type]),
                    f"IT_ANALYSIS_MAP[{artefact_type}] has drifted from the real "
                    f"ANALYSIS_MAP — update ci/integration/harness/analysis_map.py",
                )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
