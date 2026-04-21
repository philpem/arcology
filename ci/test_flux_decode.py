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
from unittest.mock import MagicMock, call, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from shared.enums import ArtefactType, AnalysisType
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
            ArtefactType.DFI: '.dfi'}.get(t, '')

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

    with patch('worker.arcworker.analysis.flux_to_imd_hxcfe', return_value=imd_result) as mock_imd, \
         patch('worker.arcworker.analysis.flux_to_hfe_hxcfe', return_value=hfe_result) as mock_hfe, \
         patch('worker.arcworker.analysis.sector_image_to_raw_greaseweazle', return_value=img_result) as mock_gw, \
         patch('worker.arcworker.analysis.parse_imd_track0', return_value=None), \
         patch('worker.arcworker.analysis.detect_geometry_from_boot_data', return_value=None):

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

def _run_flux_decode_dfi(work_dir: Path, hints: dict | None = None, mock_scp_result=None):
    """
    Run process_flux_decode for a DFI source with all external tools mocked.
    Returns (worker_mock, mock_dfi_to_scp, mock_imd, mock_hfe, mock_gw).
    """
    artefact = _make_artefact(ArtefactType.DFI)
    source_path = work_dir / artefact['storage_path']
    source_path.touch()

    scp_result = mock_scp_result or {'success': True}
    analysis = {'id': _ANALYSIS_ID, 'hints': __import__('json').dumps(hints) if hints else None}

    worker = MagicMock(spec=AnalysisWorker)
    worker.get_input_path.return_value = source_path
    worker.api = MagicMock()
    worker.api.register_derived_artefact.return_value = {'artefact': {'uuid': 'mock-uuid'}}

    with patch('worker.arcworker.analysis.dfi_to_scp_hxcfe', return_value=scp_result) as mock_dfi, \
         patch('worker.arcworker.analysis.flux_to_imd_hxcfe', return_value={'success': True}) as mock_imd, \
         patch('worker.arcworker.analysis.flux_to_hfe_hxcfe', return_value={'success': True}) as mock_hfe, \
         patch('worker.arcworker.analysis.sector_image_to_raw_greaseweazle', return_value={'success': True}) as mock_gw:

        AnalysisWorker.process_flux_decode(worker, analysis, artefact, work_dir)

    return worker, mock_dfi, mock_imd, mock_hfe, mock_gw


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

    def test_scp_has_flux_decode(self):
        from myapp.blueprints.artefacts import ANALYSIS_MAP
        self.assertIn(AnalysisType.FLUX_DECODE, ANALYSIS_MAP[ArtefactType.SCP])


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
            from myapp.database import Artefact, ArtefactType as DBArtefactType, Item, StorageDirectory
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
            from myapp.database import Analysis, AnalysisStatus
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
            from myapp.database import Artefact, Item, StorageDirectory, Analysis
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


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
