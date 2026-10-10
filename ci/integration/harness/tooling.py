"""External-tool presence checks for the integration suite.

The integration tests need real binaries (``7z``, ``unzip``, …) that only exist
in the worker container.  Outside the container the tests should *skip* with a
clear message; inside CI they must *fail* if a tool is missing rather than
silently skip — otherwise a broken image would show green.  ``ARCOLOGY_IT_STRICT``
(set in CI) flips skips into hard failures.

This mirrors the ``shutil.which`` gating pattern in ci/test_image_conversion.py.
"""

import os
import shutil
import unittest


def strict() -> bool:
    """True when missing tools must fail instead of skip (set in CI)."""
    return os.environ.get('ARCOLOGY_IT_STRICT', '') not in ('', '0', 'false', 'False')


def missing_tools(tools) -> list[str]:
    """Return the subset of *tools* not found on PATH."""
    return [t for t in tools if shutil.which(t) is None]


def require_tools(tools) -> None:
    """Skip (or, under strict mode, fail) if any required tool is absent.

    Call from a test's ``setUp``.
    """
    missing = missing_tools(tools)
    if not missing:
        return
    message = (
        f"required integration tools not on PATH: {', '.join(missing)} "
        f"(run inside the worker container, e.g. scripts/run-integration.sh)"
    )
    if strict():
        raise AssertionError(message)
    raise unittest.SkipTest(message)

# vim: ts=4 sw=4 et
