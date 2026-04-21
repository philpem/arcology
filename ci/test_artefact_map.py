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
from myapp.blueprints.artefacts import EXTENSION_MAP, ANALYSIS_MAP, detect_artefact_type

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


class TestDetectArtefactType(unittest.TestCase):
    """detect_artefact_type() handles plain and compressed extensions."""

    def _check(self, filename, expected):
        result = detect_artefact_type(filename)
        self.assertEqual(result, expected,
                         f'{filename!r} → {result} (expected {expected})')

    def test_plain_scp(self):
        self._check('disc.scp', ArtefactType.SCP)

    def test_plain_dfi(self):
        self._check('disc.dfi', ArtefactType.DFI)

    def test_dfi_gz(self):
        self._check('disc.dfi.gz', ArtefactType.DFI)

    def test_dfi_bz2(self):
        self._check('diamondmm_stealth64-video-2001_win95_v1.02_100MHz.dfi.bz2', ArtefactType.DFI)

    def test_dfi_zst(self):
        self._check('disc.dfi.zst', ArtefactType.DFI)

    def test_scp_gz(self):
        self._check('disc.scp.gz', ArtefactType.SCP)

    def test_scp_bz2(self):
        self._check('disc.scp.bz2', ArtefactType.SCP)

    def test_unknown_extension(self):
        self._check('file.xyz', ArtefactType.UNKNOWN)

    def test_compressed_unknown(self):
        self._check('file.xyz.gz', ArtefactType.UNKNOWN)

    def test_dd_bz2_explicit(self):
        self._check('drive.dd.bz2', ArtefactType.DD_BZ2)

    def test_tar_gz_explicit(self):
        self._check('archive.tar.gz', ArtefactType.TARGZ)

    def test_case_insensitive(self):
        self._check('DISC.DFI.GZ', ArtefactType.DFI)


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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
