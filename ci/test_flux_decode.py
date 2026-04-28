"""
Unit tests for process_flux_decode branching behaviour.

Verifies the four-source-type pipeline:
  SCP  → HFE sibling + IMD sibling (both skip_analyses=[FLUX_DECODE]) + RAW_SECTOR
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

from shared.enums import AnalysisType, ArtefactType
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


def _run_flux_decode(artefact_type: ArtefactType, work_dir: Path,
                     mock_imd_result=None, mock_hfe_result=None, mock_img_result=None):
    """
    Run process_flux_decode for the given source type with all external tools
    mocked out.  Returns (worker_mock, registered_calls) where registered_calls
    is the list of calls made to api.register_derived_artefact.
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
         patch('worker.arcworker.analyses.flux.detect_geometry_from_boot_data', return_value=None):

        AnalysisWorker.process_flux_decode(worker, _analysis(), artefact, work_dir)

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
        self.assertIn(AnalysisType.FLUX_DECODE.name, hfe_calls[0].kwargs.get('skip_analyses', []))

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

        AnalysisWorker.process_flux_decode(worker, analysis, artefact, work_dir)

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
        AnalysisWorker.process_detect_track_density(worker, analysis, artefact, work_dir)

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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
