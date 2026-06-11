"""
Unit tests for process_flux_decode branching behaviour.

Verifies the four-source-type pipeline:
  SCP  → HFE sibling (skip_analyses=[FLUX_DECODE, FLUX_VISUALISATION])
         + IMD sibling (skip_analyses=[FLUX_DECODE]) + RAW_SECTOR
  HFE  → IMD sibling (skip_analyses=[FLUX_DECODE]) + RAW_SECTOR; no HFE sibling
  IMD  → RAW_SECTOR only; no siblings
  DFI  → SCP sibling (no skip_analyses); SCP's own FLUX_DECODE handles the rest

External tool calls (hxcfe, greaseweazle) are fully mocked — no real images needed.

Run:
    python -m unittest ci.test_flux_decode -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from arcology_shared.enums import AnalysisType, ArtefactType
from worker.arcworker.analyses.flux import process_detect_track_density, process_flux_decode
from worker.arcworker.analysis import AnalysisWorker

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_ANALYSIS_ID = 42

def _make_artefact(artefact_type: ArtefactType, filename: str = 'disc') -> dict:
    return {
        'id': 1,
        'label': 'Test Disc',
        'artefact_type': artefact_type.value,
        'storage_path': f'{filename}{_ext(artefact_type)}',
        'storage_directory': 'uploads',
    }

def _ext(t: ArtefactType) -> str:
    return {ArtefactType.SCP: '.scp', ArtefactType.HFE: '.hfe', ArtefactType.IMD: '.imd',
            ArtefactType.DFI: '.dfi', ArtefactType.A2R: '.a2r'}.get(t, '')

def _analysis() -> dict:
    return {'id': _ANALYSIS_ID, 'hints': None}


_NOT_INDEPENDENT = {'detected': False, 'reason': 'no data on head 1', 'h1_tracks': 0, 'h1_idam_all_zero': False}


def _run_flux_decode(artefact_type: ArtefactType, work_dir: Path,
                     mock_imd_result=None, mock_hfe_result=None, mock_img_result=None):
    """
    Run process_flux_decode for the given source type with all external tools
    mocked out.  Returns (worker_mock, registered_calls) where registered_calls
    is the list of calls made to api.register_derived_artefact.

    Independent-sides detection is mocked to 'not detected' by default so
    existing tests exercise the normal merged-image path.
    """
    artefact = _make_artefact(artefact_type)
    source_path = work_dir / artefact['storage_path']
    source_path.touch()

    imd_result  = mock_imd_result  or {'success': True}
    hfe_result  = mock_hfe_result  or {'success': True}
    img_result  = mock_img_result  or {'success': True}

    worker = MagicMock(spec=AnalysisWorker)
    worker.get_input_path.return_value = source_path
    worker.api = MagicMock()
    worker.api.register_derived_artefact.return_value = {'artefact': {'uuid': 'mock-uuid'}}

    with patch('worker.arcworker.analyses.flux.flux_to_imd_hxcfe', return_value=imd_result) as mock_imd, \
         patch('worker.arcworker.analyses.flux.flux_to_hfe_hxcfe', return_value=hfe_result) as mock_hfe, \
         patch('worker.arcworker.analyses.flux.sector_image_to_raw_greaseweazle', return_value=img_result) as mock_gw, \
         patch('worker.arcworker.analyses.flux.parse_imd_track0', return_value=None), \
         patch('worker.arcworker.analyses.flux.parse_imd_tracks', return_value=None), \
         patch('worker.arcworker.analyses.flux.detect_geometry_from_boot_data', return_value=None), \
         patch('worker.arcworker.analyses.flux.detect_independent_sides', return_value=_NOT_INDEPENDENT):

        process_flux_decode(worker, _analysis(), artefact, work_dir)

    return worker, mock_imd, mock_hfe, mock_gw


# ─────────────────────────────────────────────────────────────────────────────
# SCP source
# ─────────────────────────────────────────────────────────────────────────────

class TestSCPSource(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_produces_imd_sibling(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.IMD, types_registered)

    def test_produces_hfe_sibling(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.HFE, types_registered)

    def test_produces_raw_sector(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.RAW_SECTOR, types_registered)

    def test_imd_sibling_has_skip_analyses(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        imd_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.IMD
        ]
        self.assertEqual(len(imd_calls), 1)
        self.assertIn(AnalysisType.FLUX_DECODE.name, imd_calls[0].kwargs.get('skip_analyses', []))

    def test_hfe_sibling_has_skip_analyses(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        hfe_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.HFE
        ]
        self.assertEqual(len(hfe_calls), 1)
        skip = hfe_calls[0].kwargs.get('skip_analyses', [])
        self.assertIn(AnalysisType.FLUX_DECODE.name, skip)
        # The intermediate HFE is a lossy re-encode of the SCP flux, so its
        # flux plots must not be regenerated — the SCP's own plots are kept.
        self.assertIn(AnalysisType.FLUX_VISUALISATION.name, skip)

    def test_raw_sector_has_no_skip_analyses(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        rs_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.RAW_SECTOR
        ]
        self.assertEqual(len(rs_calls), 1)
        self.assertNotIn('skip_analyses', rs_calls[0].kwargs)

    def test_gw_receives_source_path(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        gw_input = mock_gw.call_args.args[0]
        self.assertEqual(gw_input.suffix, '.scp')

    def test_calls_imd_conversion(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        self.assertTrue(mock_imd.called)

    def test_calls_hfe_conversion(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.SCP, self.work_dir)
        self.assertTrue(mock_hfe.called)


# ─────────────────────────────────────────────────────────────────────────────
# HFE source
# ─────────────────────────────────────────────────────────────────────────────

class TestHFESource(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_produces_imd_sibling(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.IMD, types_registered)

    def test_no_hfe_sibling(self):
        """Source is already HFE — no HFE sibling should be produced."""
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertNotIn(ArtefactType.HFE, types_registered)

    def test_produces_raw_sector(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.RAW_SECTOR, types_registered)

    def test_imd_sibling_has_skip_analyses(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        imd_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.IMD
        ]
        self.assertEqual(len(imd_calls), 1)
        self.assertIn(AnalysisType.FLUX_DECODE.name, imd_calls[0].kwargs.get('skip_analyses', []))

    def test_gw_receives_source_path(self):
        """gw must receive the source HFE, not the derived IMD."""
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        gw_input = mock_gw.call_args.args[0]
        self.assertEqual(gw_input.suffix, '.hfe')

    def test_no_hfe_conversion_called(self):
        """hxcfe HFE conversion must not be called (source is already HFE)."""
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        self.assertFalse(mock_hfe.called)

    def test_calls_imd_conversion(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.HFE, self.work_dir)
        self.assertTrue(mock_imd.called)


# ─────────────────────────────────────────────────────────────────────────────
# IMD source
# ─────────────────────────────────────────────────────────────────────────────

class TestIMDSource(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_imd_sibling(self):
        """Source is already IMD — no IMD sibling should be registered."""
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertNotIn(ArtefactType.IMD, types_registered)

    def test_no_hfe_sibling(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertNotIn(ArtefactType.HFE, types_registered)

    def test_produces_raw_sector(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.RAW_SECTOR, types_registered)

    def test_only_raw_sector_registered(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        self.assertEqual(worker.api.register_derived_artefact.call_count, 1)

    def test_gw_receives_source_path(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        gw_input = mock_gw.call_args.args[0]
        self.assertEqual(gw_input.suffix, '.imd')

    def test_no_imd_conversion_called(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        self.assertFalse(mock_imd.called)

    def test_no_hfe_conversion_called(self):
        worker, mock_imd, mock_hfe, mock_gw = _run_flux_decode(ArtefactType.IMD, self.work_dir)
        self.assertFalse(mock_hfe.called)


# ─────────────────────────────────────────────────────────────────────────────
# DFI source
# ─────────────────────────────────────────────────────────────────────────────

def _run_flux_decode_via_scp(
    artefact_type: ArtefactType,
    work_dir: Path,
    conversion_patch: str,
    hints: dict | None = None,
    mock_scp_result=None,
):
    """
    Generic helper for flux-to-SCP-via-conversion source types (DFI, A2R, …).

    conversion_patch is the fully-qualified name to patch for the →SCP call,
    e.g. 'worker.arcworker.analyses.flux.dfi_to_scp_hxcfe'.
    """
    ext = _ext(artefact_type)
    artefact = _make_artefact(artefact_type, f'disc{ext[1:]}')
    source_path = work_dir / artefact['storage_path']
    source_path.touch()

    scp_result = mock_scp_result or {'success': True}
    analysis = {'id': _ANALYSIS_ID, 'hints': __import__('json').dumps(hints) if hints else None}

    worker = MagicMock(spec=AnalysisWorker)
    worker.get_input_path.return_value = source_path
    worker.api = MagicMock()
    worker.api.register_derived_artefact.return_value = {'artefact': {'uuid': 'mock-uuid'}}

    with patch(conversion_patch, return_value=scp_result) as mock_conv, \
         patch('worker.arcworker.analyses.flux.flux_to_imd_hxcfe', return_value={'success': True}) as mock_imd, \
         patch('worker.arcworker.analyses.flux.flux_to_hfe_hxcfe', return_value={'success': True}) as mock_hfe, \
         patch('worker.arcworker.analyses.flux.sector_image_to_raw_greaseweazle', return_value={'success': True}) as mock_gw:

        process_flux_decode(worker, analysis, artefact, work_dir)

    return worker, mock_conv, mock_imd, mock_hfe, mock_gw


def _run_flux_decode_dfi(work_dir: Path, hints: dict | None = None, mock_scp_result=None):
    """Run process_flux_decode for a DFI source with all external tools mocked."""
    return _run_flux_decode_via_scp(
        ArtefactType.DFI, work_dir,
        'worker.arcworker.analyses.flux.dfi_to_scp_hxcfe',
        hints=hints, mock_scp_result=mock_scp_result,
    )


def _run_flux_decode_a2r(work_dir: Path, mock_scp_result=None):
    """Run process_flux_decode for an A2R source with all external tools mocked."""
    return _run_flux_decode_via_scp(
        ArtefactType.A2R, work_dir,
        'worker.arcworker.analyses.flux.a2r_to_scp_gw',
        mock_scp_result=mock_scp_result,
    )


class TestDFISource(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_calls_dfi_to_scp(self):
        """dfi_to_scp_hxcfe must be called with the DFI source path."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        self.assertTrue(mock_dfi.called)
        self.assertEqual(mock_dfi.call_args.args[0].suffix, '.dfi')

    def test_produces_scp_sibling(self):
        """SCP sibling must be registered so its own FLUX_DECODE runs."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        types_registered = [c.args[3] for c in worker.api.register_derived_artefact.call_args_list]
        self.assertIn(ArtefactType.SCP, types_registered)

    def test_scp_sibling_has_no_skip_analyses(self):
        """SCP sibling must not suppress FLUX_DECODE — it needs to run the full pipeline."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        scp_calls = [c for c in worker.api.register_derived_artefact.call_args_list
                     if c.args[3] == ArtefactType.SCP]
        self.assertEqual(len(scp_calls), 1)
        self.assertNotIn('skip_analyses', scp_calls[0].kwargs)

    def test_no_imd_hfe_gw_called(self):
        """hxcfe IMD/HFE conversion and gw must not run during DFI FLUX_DECODE."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        self.assertFalse(mock_imd.called)
        self.assertFalse(mock_hfe.called)
        self.assertFalse(mock_gw.called)

    def test_only_scp_registered(self):
        """Only the SCP sibling should be registered — no IMD, HFE, or RAW_SECTOR."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        self.assertEqual(worker.api.register_derived_artefact.call_count, 1)

    def test_clock_mhz_hint_passed_to_tool(self):
        """When dfi_clock_mhz hint is set, dfi_to_scp_hxcfe receives it."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(
            self.work_dir, hints={'dfi_clock_mhz': 100})
        self.assertEqual(mock_dfi.call_args.kwargs.get('clock_mhz'), 100)

    def test_no_clock_mhz_hint_passes_none(self):
        """Without a dfi_clock_mhz hint, clock_mhz must be None."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(self.work_dir)
        self.assertIsNone(mock_dfi.call_args.kwargs.get('clock_mhz'))

    def test_failure_propagated(self):
        """If dfi_to_scp_hxcfe fails, no sibling is registered."""
        worker, mock_dfi, mock_imd, mock_hfe, mock_gw = _run_flux_decode_dfi(
            self.work_dir, mock_scp_result={'success': False, 'error': 'hxcfe failed'})
        worker.api.register_derived_artefact.assert_not_called()
        worker.fail_analysis.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# A2R source
# ─────────────────────────────────────────────────────────────────────────────

class TestA2RSource(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_calls_a2r_to_scp(self):
        """a2r_to_scp_gw must be called with the A2R source path."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(self.work_dir)
        self.assertTrue(mock_conv.called)
        self.assertEqual(mock_conv.call_args.args[0].suffix, '.a2r')

    def test_produces_scp_sibling(self):
        """SCP sibling must be registered so its own FLUX_DECODE runs."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(self.work_dir)
        types_registered = [c.args[3] for c in worker.api.register_derived_artefact.call_args_list]
        self.assertIn(ArtefactType.SCP, types_registered)

    def test_scp_sibling_has_no_skip_analyses(self):
        """SCP sibling must not suppress FLUX_DECODE — it needs to run the full pipeline."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(self.work_dir)
        scp_calls = [c for c in worker.api.register_derived_artefact.call_args_list
                     if c.args[3] == ArtefactType.SCP]
        self.assertEqual(len(scp_calls), 1)
        self.assertNotIn('skip_analyses', scp_calls[0].kwargs)

    def test_no_imd_hfe_gw_called(self):
        """hxcfe IMD/HFE conversion and gw sector conversion must not run during A2R FLUX_DECODE."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(self.work_dir)
        self.assertFalse(mock_imd.called)
        self.assertFalse(mock_hfe.called)
        self.assertFalse(mock_gw.called)

    def test_only_scp_registered(self):
        """Only the SCP sibling should be registered — no IMD, HFE, or RAW_SECTOR."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(self.work_dir)
        self.assertEqual(worker.api.register_derived_artefact.call_count, 1)

    def test_failure_propagated(self):
        """If a2r_to_scp_gw fails, no sibling is registered."""
        worker, mock_conv, mock_imd, mock_hfe, mock_gw = _run_flux_decode_a2r(
            self.work_dir, mock_scp_result={'success': False, 'error': 'gw failed'})
        worker.api.register_derived_artefact.assert_not_called()
        worker.fail_analysis.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS_MAP ping-pong prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalysisMapFluxDecode(unittest.TestCase):
    """Verify FLUX_DECODE is now present for HFE and IMD in ANALYSIS_MAP."""

    def test_hfe_has_flux_decode(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        self.assertIn(AnalysisType.FLUX_DECODE, ANALYSIS_MAP[ArtefactType.HFE])

    def test_imd_has_flux_decode(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        self.assertIn(AnalysisType.FLUX_DECODE, ANALYSIS_MAP[ArtefactType.IMD])

    def test_scp_flux_decode_not_in_analysis_map(self):
        # FLUX_DECODE is intentionally absent from the SCP ANALYSIS_MAP entry.
        # DETECT_TRACK_DENSITY queues it explicitly on the correct target image
        # (original or density-corrected SCP) to prevent duplicate processing.
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        self.assertNotIn(AnalysisType.FLUX_DECODE, ANALYSIS_MAP[ArtefactType.SCP])

    def test_scp_has_detect_track_density(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        self.assertIn(AnalysisType.DETECT_TRACK_DENSITY, ANALYSIS_MAP[ArtefactType.SCP])


# ─────────────────────────────────────────────────────────────────────────────
# queue_analyses_for_artefact skip_analyses
# ─────────────────────────────────────────────────────────────────────────────

class TestSkipAnalyses(unittest.TestCase):
    """Verify skip_analyses suppresses the named types."""

    def _app(self):
        import os
        os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
        os.environ.setdefault('SECRET_KEY', 'test')
        from myapp.app import create_app
        app = create_app()
        app.config.update({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
            'SECRET_KEY': 'test',
            'WTF_CSRF_ENABLED': False,
        })
        return app

    def test_skip_analyses_excludes_type(self):
        """FLUX_DECODE must not be queued when listed in skip_analyses."""
        app = self._app()
        with app.app_context():
            from myapp.extensions import db
            db.create_all()
            from myapp.blueprints.artefacts import queue_analyses_for_artefact
            from myapp.database import Analysis, Artefact, Item, StorageDirectory
            item = Item(name='test item')
            db.session.add(item)
            db.session.flush()
            artefact = Artefact(
                item_id=item.id,
                label='test',
                original_filename='x.hfe',
                artefact_type=ArtefactType.HFE,
                storage_path='x.hfe',
                storage_directory=StorageDirectory.OUTPUTS,
            )
            db.session.add(artefact)
            db.session.flush()

            # Call with skip_analyses — should not queue FLUX_DECODE
            queue_analyses_for_artefact(
                artefact,
                skip_analyses=[AnalysisType.FLUX_DECODE.name],
                skip_duplicate_check=True,
            )
            db.session.flush()
            queued = {a.analysis_type for a in Analysis.query.filter_by(artefact_id=artefact.id).all()}
            self.assertNotIn(AnalysisType.FLUX_DECODE, queued)

    def test_without_skip_analyses_flux_decode_queued(self):
        """Without skip_analyses, FLUX_DECODE IS queued for HFE."""
        app = self._app()
        with app.app_context():
            from myapp.extensions import db
            db.create_all()
            from myapp.blueprints.artefacts import queue_analyses_for_artefact
            from myapp.database import Analysis, Artefact, Item, StorageDirectory
            item = Item(name='test item 2')
            db.session.add(item)
            db.session.flush()
            artefact = Artefact(
                item_id=item.id,
                label='test2',
                original_filename='y.hfe',
                artefact_type=ArtefactType.HFE,
                storage_path='y.hfe',
                storage_directory=StorageDirectory.OUTPUTS,
            )
            db.session.add(artefact)
            db.session.flush()

            queue_analyses_for_artefact(artefact, skip_duplicate_check=True)
            db.session.flush()
            queued = {a.analysis_type for a in Analysis.query.filter_by(artefact_id=artefact.id).all()}
            self.assertIn(AnalysisType.FLUX_DECODE, queued)


# ─────────────────────────────────────────────────────────────────────────────
# process_detect_track_density downstream queueing
# ─────────────────────────────────────────────────────────────────────────────

def _run_detect_track_density(work_dir: Path, *, mismatch_detected: bool,
                               fix_success: bool = True,
                               imd_success: bool = True):
    """
    Run process_detect_track_density with all external tools mocked.

    Returns the worker mock so callers can inspect api.queue_analysis calls.
    """
    artefact = {
        'id': 1,
        'uuid': 'original-uuid',
        'label': 'Test SCP',
        'artefact_type': ArtefactType.SCP.value,
        'storage_path': 'disc.scp',
        'storage_directory': 'uploads',
    }
    analysis = {'id': 42, 'hints': None}

    source_path = work_dir / 'disc.scp'
    source_path.touch()

    worker = MagicMock(spec=AnalysisWorker)
    worker.get_input_path.return_value = source_path
    worker.api = MagicMock()
    worker.api.register_derived_artefact.return_value = {
        'artefact': {'uuid': 'corrected-uuid'}
    }

    detection_result = {
        'detected': mismatch_detected,
        'confidence': 0.95 if mismatch_detected else 0.0,
        'checked': 40 if mismatch_detected else 0,
        'matching': 40 if mismatch_detected else 0,
        'data_heads': [0] if mismatch_detected else [],
        'blank_heads': [1] if mismatch_detected else [],
        'odd_tracks_with_duplicate_data': 0,
        'odd_tracks_with_varied_data': 0,
        'odd_tracks_with_uniform_data': 0,
    }

    imd_result = {'success': imd_success}
    fix_result = {'success': fix_success}

    with patch('worker.arcworker.analyses.flux.flux_to_imd_hxcfe', return_value=imd_result), \
         patch('worker.arcworker.analyses.flux.parse_imd_tracks',
               return_value=[{'physical_index': 0}] if imd_success else None), \
         patch('worker.arcworker.analyses.flux.detect_track_density_mismatch',
               return_value=detection_result), \
         patch('worker.arcworker.analyses.flux.scp_fix_track_density', return_value=fix_result):
        process_detect_track_density(worker, analysis, artefact, work_dir)

    return worker


class TestDetectTrackDensityDownstreamQueueing(unittest.TestCase):
    """
    process_detect_track_density must queue FLUX_VISUALISATION and FLUX_DECODE on
    the correct SCP target — original when no mismatch, corrected when 40-in-80
    detected — to prevent duplicate HFE/IMD/RAW_SECTOR artefacts.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_mismatch_queues_flux_visualisation_on_original(self):
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=False)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        types = [c[1] for c in calls]
        self.assertIn('original-uuid', uuids)
        self.assertIn(AnalysisType.FLUX_VISUALISATION.value, types)

    def test_no_mismatch_queues_flux_decode_on_original(self):
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=False)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        types = [c[1] for c in calls]
        self.assertIn('original-uuid', uuids)
        self.assertIn(AnalysisType.FLUX_DECODE.value, types)

    def test_no_mismatch_does_not_queue_on_corrected_uuid(self):
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=False)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        self.assertNotIn('corrected-uuid', uuids)

    def test_mismatch_queues_flux_visualisation_on_corrected(self):
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=True)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        types = [c[1] for c in calls]
        self.assertIn('corrected-uuid', uuids)
        self.assertIn(AnalysisType.FLUX_VISUALISATION.value, types)

    def test_mismatch_queues_flux_decode_on_corrected(self):
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=True)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        types = [c[1] for c in calls]
        self.assertIn('corrected-uuid', uuids)
        self.assertIn(AnalysisType.FLUX_DECODE.value, types)

    def test_mismatch_does_not_queue_on_original(self):
        """When a corrected SCP is created, the original must not enter the decode pipeline."""
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=True)
        calls = [c.args for c in worker.api.queue_analysis.call_args_list]
        uuids = [c[0] for c in calls]
        self.assertNotIn('original-uuid', uuids)

    def test_mismatch_corrected_scp_skips_detect_track_density(self):
        """The corrected SCP must skip DETECT_TRACK_DENSITY to avoid re-detection."""
        worker = _run_detect_track_density(self.work_dir, mismatch_detected=True)
        register_calls = worker.api.register_derived_artefact.call_args_list
        self.assertEqual(len(register_calls), 1)
        kwargs = register_calls[0].kwargs
        skip = kwargs.get('skip_analyses', [])
        self.assertIn(AnalysisType.DETECT_TRACK_DENSITY.name, skip)


# ─────────────────────────────────────────────────────────────────────────────
# Independent sides: detect_independent_sides() unit tests
# ─────────────────────────────────────────────────────────────────────────────

from worker.arcworker.tools.imd import detect_independent_sides


def _make_tracks(num_cylinders: int, h1_idam_zero: bool, h0_has_data: bool = True) -> list[dict]:
    """
    Build a synthetic track list for detect_independent_sides() tests.

    h1_idam_zero=True  → each physical-head-1 track has idam_heads=[0, 0, …]  (independent sides)
    h1_idam_zero=False → each physical-head-1 track has idam_heads=[1, 1, …]  (normal DS)
    """
    tracks = []
    for cyl in range(num_cylinders):
        # Physical head 0 — IDAM head always 0
        tracks.append({
            'physical_index': cyl * 2,
            'cylinder': cyl,
            'head': 0,
            'encoding': 'FM',
            'sector_size': 256,
            'sector_ids': list(range(1, 11)),
            'sector_cyls': [cyl] * 10,
            'idam_heads': [0] * 10,
            'has_data': h0_has_data,
            'is_uniform_fill': False,
        })
        # Physical head 1
        idam_head_val = 0 if h1_idam_zero else 1
        tracks.append({
            'physical_index': cyl * 2 + 1,
            'cylinder': cyl,
            'head': 1,
            'encoding': 'FM',
            'sector_size': 256,
            'sector_ids': list(range(1, 11)),
            'sector_cyls': [cyl] * 10,
            'idam_heads': [idam_head_val] * 10,
            'has_data': True,
            'is_uniform_fill': False,
        })
    return tracks


class TestDetectIndependentSides(unittest.TestCase):
    """Unit tests for detect_independent_sides()."""

    def test_independent_sides_detected(self):
        """Both heads present, head-1 IDAM head=0 → independent sides."""
        tracks = _make_tracks(40, h1_idam_zero=True)
        result = detect_independent_sides(tracks)
        self.assertTrue(result['detected'])

    def test_normal_ds_not_detected(self):
        """Both heads present, head-1 IDAM head=1 → normal DS, not independent."""
        tracks = _make_tracks(40, h1_idam_zero=False)
        result = detect_independent_sides(tracks)
        self.assertFalse(result['detected'])

    def test_single_sided_not_detected(self):
        """Only head 0 has data → single-sided, not independent."""
        tracks = _make_tracks(40, h1_idam_zero=True, h0_has_data=True)
        # Override head-1 tracks to have no data
        for t in tracks:
            if t['head'] == 1:
                t['has_data'] = False
        result = detect_independent_sides(tracks)
        self.assertFalse(result['detected'])

    def test_h1_tracks_count_reported(self):
        """h1_tracks in result equals the number of head-1 tracks with data."""
        tracks = _make_tracks(40, h1_idam_zero=True)
        result = detect_independent_sides(tracks)
        self.assertEqual(result['h1_tracks'], 40)

    def test_h1_idam_all_zero_flag(self):
        """h1_idam_all_zero is True when all head-1 sectors carry IDAM head=0."""
        tracks = _make_tracks(40, h1_idam_zero=True)
        result = detect_independent_sides(tracks)
        self.assertTrue(result['h1_idam_all_zero'])

    def test_mixed_idam_heads_not_detected(self):
        """If even one head-1 sector has IDAM head=1, independent sides are not detected."""
        tracks = _make_tracks(40, h1_idam_zero=True)
        # Corrupt one sector's IDAM head on a head-1 track
        for t in tracks:
            if t['head'] == 1:
                t['idam_heads'] = [0] * 9 + [1]
                break
        result = detect_independent_sides(tracks)
        self.assertFalse(result['detected'])


# ─────────────────────────────────────────────────────────────────────────────
# Independent sides: process_flux_decode() integration tests
# ─────────────────────────────────────────────────────────────────────────────

_INDEPENDENT_SIDES_DETECTED = {
    'detected': True,
    'reason': 'head 1 IDAM all zero',
    'h1_tracks': 40,
    'h1_idam_all_zero': True,
}

_DFS_SS80_GEOMETRY = {
    'filesystem': 'dfs',
    'cylinders': 80,
    'heads': 1,          # single-sided geometry (each side taken individually)
    'sectors_per_track': 10,
    'sector_size': 256,
    'encoding': 'FM',
    'probe': 'A',
}

# The merged track-0 probe reports heads=2 (physical disc has 2 heads).
_DFS_DS80_GEOMETRY = {**_DFS_SS80_GEOMETRY, 'heads': 2}


def _run_flux_decode_independent_sides(work_dir: Path, independent_sides_detected: bool,
                               geometry: dict | None = None,
                               mock_side_result=None,
                               sides_identical: bool = False):
    """
    Run process_flux_decode for an SCP source with independent-sides detection either
    detected or not.  Returns the worker mock.

    The mocked one-side gw conversion writes a real file for each head so that
    the worker's identical-sides hash comparison has something to hash.  By
    default the two sides get distinct content (so both register); pass
    sides_identical=True to make them byte-identical (blank-disc case).
    """
    artefact = _make_artefact(ArtefactType.SCP)
    source_path = work_dir / artefact['storage_path']
    source_path.touch()

    side_result = mock_side_result or {'success': True}
    per_side = _INDEPENDENT_SIDES_DETECTED if independent_sides_detected else _NOT_INDEPENDENT

    def _one_side_side_effect(input_path, output_path, gw_format, head, cylinders):
        # Materialise the side image so compute_file_hash() can read it.
        content = b'IDENTICAL' if sides_identical else f'SIDE{head}'.encode()
        Path(output_path).write_bytes(content)
        return side_result

    worker = MagicMock(spec=AnalysisWorker)
    worker.get_input_path.return_value = source_path
    worker.api = MagicMock()
    worker.api.register_derived_artefact.return_value = {'artefact': {'uuid': 'mock-uuid'}}

    # Return a non-empty (truthy) track list so that detect_independent_sides is reached.
    # Return a proper stub track0 so detect_geometry_from_boot_data is invoked when
    # a geometry override is provided; the actual content is irrelevant since
    # detect_geometry_from_boot_data is also mocked.
    stub_tracks = [{'stub': True}]
    stub_track0 = (
        {'encoding': 'FM', 'sector_size': 256, 'cylinders': 80, 'heads': 2, 'sectors': {}}
        if geometry is not None else None
    )
    with patch('worker.arcworker.analyses.flux.flux_to_imd_hxcfe', return_value={'success': True}), \
         patch('worker.arcworker.analyses.flux.flux_to_hfe_hxcfe', return_value={'success': True}), \
         patch('worker.arcworker.analyses.flux.sector_image_to_raw_greaseweazle', return_value={'success': True}) as mock_merged_gw, \
         patch('worker.arcworker.analyses.flux.sector_image_to_raw_greaseweazle_one_side', side_effect=_one_side_side_effect) as mock_side_gw, \
         patch('worker.arcworker.analyses.flux.parse_imd_track0', return_value=stub_track0), \
         patch('worker.arcworker.analyses.flux.parse_imd_tracks', return_value=stub_tracks), \
         patch('worker.arcworker.analyses.flux.detect_geometry_from_boot_data', return_value=geometry), \
         patch('worker.arcworker.analyses.flux.detect_independent_sides', return_value=per_side):

        process_flux_decode(worker, _analysis(), artefact, work_dir)

    return worker, mock_merged_gw, mock_side_gw


class TestIndependentSidesSplit(unittest.TestCase):
    """
    process_flux_decode must split independent-sides discs into two single-sided
    RAW_SECTOR artefacts and must NOT register the merged RAW_SECTOR.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_raw_sector_artefacts_registered(self):
        """Two RAW_SECTOR derived artefacts are registered when independent sides detected."""
        worker, _, _ = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        raw_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.RAW_SECTOR
        ]
        self.assertEqual(len(raw_calls), 2)

    def test_side_labels_contain_side_number(self):
        """Registered artefact labels include '(Side 0)' and '(Side 1)'."""
        worker, _, _ = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        labels = [
            c.args[1] for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.RAW_SECTOR
        ]
        self.assertTrue(any('Side 0' in label for label in labels), labels)
        self.assertTrue(any('Side 1' in label for label in labels), labels)

    def test_side_partition_index_base_hint(self):
        """Each side artefact is tagged with partition_index_base = its side number.

        This makes the parent disc's aggregated partition list read
        'partition 0' / 'partition 1' instead of two indistinguishable '0's.
        """
        worker, _, _ = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        bases = {
            c.args[1]: c.kwargs.get('analysis_hints', {}).get('partition_index_base')
            for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.RAW_SECTOR
        }
        side0 = next(v for k, v in bases.items() if 'Side 0' in k)
        side1 = next(v for k, v in bases.items() if 'Side 1' in k)
        self.assertEqual(side0, 0)
        self.assertEqual(side1, 1)

    def test_merged_raw_sector_not_registered_when_independent(self):
        """The merged single-image gw convert must NOT be called when independent sides detected."""
        worker, mock_merged_gw, _ = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        mock_merged_gw.assert_not_called()

    def test_one_side_gw_call_per_head(self):
        """sector_image_to_raw_greaseweazle_one_side is called twice (head 0 and head 1)."""
        worker, _, mock_side_gw = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        self.assertEqual(mock_side_gw.call_count, 2)
        heads_used = [c.args[3] for c in mock_side_gw.call_args_list]
        self.assertIn(0, heads_used)
        self.assertIn(1, heads_used)

    def test_single_sided_gw_format_chosen(self):
        """Single-sided format (acorn.dfs.ss80) selected when geometry is DS DFS 80-track."""
        worker, _, mock_side_gw = _run_flux_decode_independent_sides(
            self.work_dir, independent_sides_detected=True, geometry=_DFS_DS80_GEOMETRY
        )
        formats_used = [c.args[2] for c in mock_side_gw.call_args_list]
        for fmt in formats_used:
            self.assertEqual(fmt, 'acorn.dfs.ss80', f"Expected ss80 format, got {fmt!r}")

    def test_normal_disc_uses_merged_path(self):
        """Normal (non-independent) disc produces one merged RAW_SECTOR via gw."""
        worker, mock_merged_gw, mock_side_gw = _run_flux_decode_independent_sides(
            self.work_dir, independent_sides_detected=False
        )
        mock_merged_gw.assert_called_once()
        mock_side_gw.assert_not_called()

    def test_hfe_and_imd_siblings_always_registered(self):
        """HFE and IMD siblings are produced regardless of independent-sides detection."""
        worker, _, _ = _run_flux_decode_independent_sides(self.work_dir, independent_sides_detected=True)
        types_registered = [
            c.args[3] for c in worker.api.register_derived_artefact.call_args_list
        ]
        self.assertIn(ArtefactType.HFE, types_registered)
        self.assertIn(ArtefactType.IMD, types_registered)

    def test_identical_sides_register_single_artefact(self):
        """Byte-identical sides (e.g. blank disc) register ONE combined RAW_SECTOR.

        Two identical-content artefacts cannot coexist under the (item_id, sha256)
        uniqueness constraint, so the split must collapse to a single artefact
        rather than letting the second registration collide and re-home the first.
        """
        worker, _, mock_side_gw = _run_flux_decode_independent_sides(
            self.work_dir, independent_sides_detected=True, sides_identical=True
        )
        # Both sides are still converted...
        self.assertEqual(mock_side_gw.call_count, 2)
        # ...but only one RAW_SECTOR artefact is registered, labelled as combined.
        raw_calls = [
            c for c in worker.api.register_derived_artefact.call_args_list
            if c.args[3] == ArtefactType.RAW_SECTOR
        ]
        self.assertEqual(len(raw_calls), 1)
        self.assertIn('identical', raw_calls[0].args[1].lower())


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
