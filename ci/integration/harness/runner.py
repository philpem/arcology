"""Glue between a test case, the driver, normalisation and the golden file.

``check_case`` runs a fixture, normalises the result, and either rewrites the
golden (regen mode) or asserts byte-for-byte equality with a readable unified
diff on mismatch.
"""

import difflib
import json
import os
from pathlib import Path
from .driver import PipelineDriver
from .normalise import normalise

INTEGRATION_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = INTEGRATION_DIR / 'fixtures'
GOLDENS_DIR = INTEGRATION_DIR / 'goldens'


def regen_enabled() -> bool:
    return os.environ.get('ARCOLOGY_IT_REGEN', '') not in ('', '0', 'false', 'False')


def list_fixture_cases() -> list[str]:
    return sorted(
        p.parent.name for p in FIXTURES_DIR.glob('*/manifest.json')
    )


def run_normalised(case_name: str) -> dict:
    driver = PipelineDriver(FIXTURES_DIR / case_name)
    result = driver.run()
    return normalise(result, driver.roots)


def _dump(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + '\n'


def check_case(test, case_name: str) -> None:
    """Run *case_name* and compare to (or regenerate) its golden."""
    actual = _dump(run_normalised(case_name))
    golden_path = GOLDENS_DIR / f'{case_name}.expected.json'

    if regen_enabled():
        GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual)
        return

    test.assertTrue(
        golden_path.exists(),
        f"missing golden {golden_path} — run with ARCOLOGY_IT_REGEN=1 to create it",
    )
    expected = golden_path.read_text()
    if actual != expected:
        diff = ''.join(difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f'{case_name}.expected.json',
            tofile=f'{case_name}.actual',
        ))
        test.fail(
            f"golden mismatch for {case_name} "
            f"(regen with ARCOLOGY_IT_REGEN=1):\n{diff}"
        )

# vim: ts=4 sw=4 et
