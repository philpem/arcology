"""
Unit tests for EXTENSION_MAP and ANALYSIS_MAP in myapp/blueprints/artefacts.py.

Checks that every enum value referenced in the maps actually exists in
shared/enums.py, and that every ArtefactType has an ANALYSIS_MAP entry.
Catches the common mistake of adding a new type to the enum but forgetting
to update one of the maps.

Requires pip install (Flask, SQLAlchemy, etc. must be available).

Run:
    python -m unittest ci.test_artefact_map -v
"""

import os
import sys
import unittest

# Ensure the repo root is on sys.path so ``myapp`` and ``shared`` are importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# shared.enums has no external dependencies — import directly for the
# canonical set of valid enum members.
from shared.enums import ArtefactType, AnalysisType

# myapp.blueprints.artefacts imports Flask/SQLAlchemy at module level.
# Those packages are available in the app-tests CI job (pip install is done
# before these tests run).
from myapp.blueprints.artefacts import EXTENSION_MAP, ANALYSIS_MAP

_ALL_ARTEFACT_TYPES = set(ArtefactType)
_ALL_ANALYSIS_TYPES = set(AnalysisType)


class TestExtensionMap(unittest.TestCase):
    """EXTENSION_MAP maps file extensions → ArtefactType."""

    def test_values_are_valid_artefact_types(self):
        """Every value in EXTENSION_MAP must be a member of ArtefactType."""
        invalid = {
            ext: val
            for ext, val in EXTENSION_MAP.items()
            if val not in _ALL_ARTEFACT_TYPES
        }
        self.assertFalse(
            invalid,
            f'EXTENSION_MAP values not in ArtefactType: {invalid}',
        )

    def test_keys_start_with_dot_or_comma(self):
        """Extension keys should start with '.' or ',' (RISC OS comma-suffix)."""
        bad = [k for k in EXTENSION_MAP if not k.startswith(('.', ','))]
        self.assertFalse(bad, f'EXTENSION_MAP keys without leading dot or comma: {bad}')

    def test_keys_are_lowercase(self):
        """Extension keys should be lowercase for case-insensitive matching."""
        bad = [k for k in EXTENSION_MAP if k != k.lower()]
        self.assertFalse(bad, f'EXTENSION_MAP keys not lowercase: {bad}')


class TestAnalysisMap(unittest.TestCase):
    """ANALYSIS_MAP maps ArtefactType → list[AnalysisType]."""

    def test_keys_are_valid_artefact_types(self):
        """Every key in ANALYSIS_MAP must be a member of ArtefactType."""
        invalid = [k for k in ANALYSIS_MAP if k not in _ALL_ARTEFACT_TYPES]
        self.assertFalse(
            invalid,
            f'ANALYSIS_MAP keys not in ArtefactType: {invalid}',
        )

    def test_values_are_valid_analysis_types(self):
        """Every item in every value list must be a member of AnalysisType."""
        invalid = {}
        for artefact_type, analysis_list in ANALYSIS_MAP.items():
            bad = [a for a in analysis_list if a not in _ALL_ANALYSIS_TYPES]
            if bad:
                invalid[artefact_type] = bad
        self.assertFalse(
            invalid,
            f'ANALYSIS_MAP values not in AnalysisType: {invalid}',
        )

    def test_values_are_non_empty_lists(self):
        """Every ArtefactType entry should map to at least one analysis."""
        empty = [k for k, v in ANALYSIS_MAP.items() if not v]
        self.assertFalse(empty, f'ANALYSIS_MAP entries with empty lists: {empty}')

    def test_all_artefact_types_covered(self):
        """Every ArtefactType member should have an entry in ANALYSIS_MAP.

        A missing entry means a newly-added type will silently get no
        automatic analysis queued on upload.
        """
        missing = _ALL_ARTEFACT_TYPES - set(ANALYSIS_MAP.keys())
        self.assertFalse(
            missing,
            f'ArtefactType members not in ANALYSIS_MAP: {missing}\n'
            'Add an entry to ANALYSIS_MAP in myapp/blueprints/artefacts.py.',
        )

    def test_sector_image_types_queue_flux_decode(self):
        """HFE and IMD must queue FLUX_DECODE so they get decoded to a
        RAW_SECTOR derived artefact.  Without this, uploading an HFE or IMD
        as a root artefact never triggers PARTITION_DETECT / FILE_EXTRACTION
        (see bug #120)."""
        for artefact_type in (ArtefactType.HFE, ArtefactType.IMD):
            with self.subTest(artefact_type=artefact_type):
                self.assertIn(
                    AnalysisType.FLUX_DECODE,
                    ANALYSIS_MAP.get(artefact_type, []),
                    f'{artefact_type.name} must include FLUX_DECODE so it is '
                    'decoded to a RAW_SECTOR derived artefact for downstream '
                    'file extraction.',
                )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
