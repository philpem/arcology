"""
Regression test for worker/arcworker/analysis.py::process_flux_decode().

Locks in the following invariants (see bug #120 and its follow-up):

  - An SCP (flux) source may produce IMD, HFE, and RAW_SECTOR derived
    artefacts.
  - An HFE source may produce an IMD derived artefact (via HxCFE's
    auto-detecting converter) and must produce a RAW_SECTOR derived
    artefact.  It must NOT produce an HFE derived artefact (same-format
    loop).
  - An IMD source must only produce a RAW_SECTOR derived artefact.  It
    must NOT produce an HFE derived artefact — that would re-queue
    FLUX_DECODE via ANALYSIS_MAP[HFE] and ping-pong IMD↔HFE.
  - A source whose artefact_type is not supported must fail the analysis
    without calling any tool or registering any derived artefact.
  - The handler's internal `imd_result` variable is always a dict
    (never None) with a boolean `'success'` key so downstream geometry-
    probing code can safely do `imd_result['success']` without guarding
    against None — this contract is how a worker merged with the
    in-flight `gw_format` geometry detection stays non-crashing.

Run:
    WORKER_API_KEY=test python -m unittest ci.test_flux_decode -v
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

from shared.enums import ArtefactType, AnalysisType
from worker.arcworker import analysis as analysis_module
from worker.arcworker.analysis import AnalysisWorker


def _ok(output_path, output_type):
    return {
        'success': True,
        'tool': 'mock',
        'output_path': str(output_path),
        'output_type': output_type.value,
        'summary': 'mock ok',
        'process_output': '',
    }


class FluxDecodeTestBase(unittest.TestCase):
    """Set up an AnalysisWorker with its external collaborators mocked."""

    def setUp(self):
        self.worker = AnalysisWorker(
            api_url='http://mock.invalid',
            upload_dir=Path('/tmp/mock-uploads'),
            output_dir=Path('/tmp/mock-outputs'),
            api_key='test',
        )
        self.worker.api = mock.MagicMock()
        self.worker.api.register_derived_artefact.return_value = {'id': 1}

        # Patch the handler's tool call-sites and the worker's report /
        # input helpers so the test does no disk I/O and no subprocess
        # execution.
        patches = [
            mock.patch.object(
                self.worker, 'get_input_path',
                side_effect=lambda artefact, wd: wd / 'source',
            ),
            mock.patch.object(self.worker, 'complete_analysis'),
            mock.patch.object(self.worker, 'fail_analysis'),
            mock.patch.object(
                analysis_module, 'flux_to_imd_hxcfe',
                side_effect=lambda src, dst: _ok(dst, ArtefactType.IMD),
            ),
            mock.patch.object(
                analysis_module, 'flux_to_hfe_hxcfe',
                side_effect=lambda src, dst: _ok(dst, ArtefactType.HFE),
            ),
            mock.patch.object(
                analysis_module, 'sector_image_to_raw_greaseweazle',
                side_effect=lambda src, dst: _ok(dst, ArtefactType.RAW_SECTOR),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.imd_tool = analysis_module.flux_to_imd_hxcfe
        self.hfe_tool = analysis_module.flux_to_hfe_hxcfe
        self.gw_tool = analysis_module.sector_image_to_raw_greaseweazle

    def _run(self, source_type: ArtefactType, work_dir: Path | None = None) -> list[ArtefactType]:
        """Invoke process_flux_decode with a mock artefact of the given type.

        Returns the list of ArtefactType values passed to
        register_derived_artefact (positional arg 3).
        """
        analysis = {'id': 42}
        artefact = {
            'id': 1,
            'uuid': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'label': 'Mock artefact',
            'artefact_type': source_type.value,
        }
        self.worker.process_flux_decode(analysis, artefact, work_dir or Path('/tmp/mock-work'))

        return [
            call.args[3]
            for call in self.worker.api.register_derived_artefact.call_args_list
        ]


class TestFluxDecodeSCPSource(FluxDecodeTestBase):

    def test_scp_produces_imd_hfe_and_raw_sector(self):
        derived_types = self._run(ArtefactType.SCP)
        self.assertIn(ArtefactType.IMD, derived_types)
        self.assertIn(ArtefactType.HFE, derived_types)
        self.assertIn(ArtefactType.RAW_SECTOR, derived_types)
        self.worker.complete_analysis.assert_called_once()
        self.worker.fail_analysis.assert_not_called()


class TestFluxDecodeHFESource(FluxDecodeTestBase):

    def test_hfe_source_produces_imd_and_raw_sector_but_not_hfe(self):
        derived_types = self._run(ArtefactType.HFE)

        self.assertIn(
            ArtefactType.IMD, derived_types,
            'HFE source should produce an IMD derived artefact (HxCFE '
            'auto-detects HFE input).  FLUX_DECODE is suppressed on the '
            'derived IMD so that Greaseweazle running directly on the '
            'source HFE does not produce a duplicate RAW_SECTOR.',
        )
        self.assertIn(
            ArtefactType.RAW_SECTOR, derived_types,
            'HFE source must produce a RAW_SECTOR derived artefact so '
            'downstream PARTITION_DETECT / FILE_EXTRACTION can run.',
        )
        self.assertNotIn(
            ArtefactType.HFE, derived_types,
            'HFE source must not produce an HFE derived artefact '
            '(same-format loop).',
        )

    def test_hfe_source_invokes_only_the_imd_hxcfe_tool(self):
        """HFE sources should run HxCFE's IMD conversion but NOT its HFE
        conversion (that would re-derive the same format)."""
        self._run(ArtefactType.HFE)
        self.imd_tool.assert_called_once()
        self.hfe_tool.assert_not_called()
        self.gw_tool.assert_called_once()

    def test_hfe_derived_imd_suppresses_flux_decode(self):
        """When registering the derived IMD from an HFE source, skip_analyses
        must include FLUX_DECODE so that the IMD does not auto-queue another
        Greaseweazle run and produce a duplicate RAW_SECTOR artefact."""
        self._run(ArtefactType.HFE)
        # Find the register_derived_artefact call that registered the IMD.
        imd_calls = [
            c for c in self.worker.api.register_derived_artefact.call_args_list
            if c.args[3] is ArtefactType.IMD
        ]
        self.assertEqual(len(imd_calls), 1, 'Expected exactly one IMD registration')
        skip = imd_calls[0].kwargs.get('skip_analyses', [])
        self.assertIn(
            AnalysisType.FLUX_DECODE, skip,
            'FLUX_DECODE must be in skip_analyses for the derived IMD so '
            'that it does not re-queue Greaseweazle and produce a second '
            'RAW_SECTOR artefact (bug #120 follow-up).',
        )


class TestFluxDecodeIMDSource(FluxDecodeTestBase):

    def test_imd_source_only_registers_raw_sector_derived(self):
        derived_types = self._run(ArtefactType.IMD)

        self.assertEqual(
            derived_types, [ArtefactType.RAW_SECTOR],
            'IMD source must produce exactly one derived artefact '
            '(RAW_SECTOR) — producing an HFE sibling would re-queue '
            'FLUX_DECODE via ANALYSIS_MAP[HFE] and ping-pong IMD↔HFE '
            '(bug #120 follow-up); producing an IMD sibling would be '
            'a no-op duplicate.',
        )

    def test_imd_source_does_not_call_hxcfe(self):
        self._run(ArtefactType.IMD)
        self.imd_tool.assert_not_called()
        self.hfe_tool.assert_not_called()
        self.gw_tool.assert_called_once()


class TestFluxDecodeUnsupportedSource(FluxDecodeTestBase):

    def test_unsupported_source_fails_without_side_effects(self):
        # ZIP is a valid ArtefactType member but not a valid FLUX_DECODE input.
        self._run(ArtefactType.ZIP)
        self.worker.fail_analysis.assert_called_once()
        self.worker.complete_analysis.assert_not_called()
        self.worker.api.register_derived_artefact.assert_not_called()
        self.imd_tool.assert_not_called()
        self.hfe_tool.assert_not_called()
        self.gw_tool.assert_not_called()


class TestGreaseweazleInputForEachSource(FluxDecodeTestBase):
    """Greaseweazle reads SCP, HFE, and IMD natively, so for every
    supported source type it must be fed the source artefact directly —
    never an HxCFE-decoded intermediate (that would cook the data
    twice).  This also implicitly exercises the contract that the
    handler's `imd_result` is always indexable by 'success', so a
    downstream geometry-probing patch (`if imd_result['success']: …`)
    cannot reintroduce `TypeError: 'NoneType' object is not subscriptable`.
    """

    def _gw_first_arg(self):
        self.gw_tool.assert_called_once()
        return self.gw_tool.call_args.args[0]

    def test_scp_feeds_greaseweazle_the_source_itself(self):
        self._run(ArtefactType.SCP)
        img_input = self._gw_first_arg()
        self.assertIsNotNone(img_input)
        self.assertEqual(img_input.name, 'source')

    def test_imd_feeds_greaseweazle_the_source_itself(self):
        self._run(ArtefactType.IMD)
        img_input = self._gw_first_arg()
        self.assertIsNotNone(img_input)
        self.assertEqual(img_input.name, 'source')

    def test_hfe_feeds_greaseweazle_the_source_itself(self):
        self._run(ArtefactType.HFE)
        img_input = self._gw_first_arg()
        self.assertIsNotNone(img_input)
        self.assertEqual(img_input.name, 'source')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
