"""
MS Word document → text conversion and detection wiring.

Exercises the dialect-independent, tool-free parts of Track 1 of the document
full-text plan:
  - word_to_text() on a synthetic .docx (stdlib OOXML path — no external tool),
    including paragraph / tab / line-break handling and the zip-bomb guard.
  - graceful failure of the legacy .doc path when antiword/catdoc are absent
    (they live only in the worker image, not the app-tests environment).
  - the detection wiring: .doc/.docx map to MS_WORD for direct upload
    (EXTENSION_MAP), extraction scan (viewable_artefact_type / classify_content),
    and queue MS_WORD → FORMAT_CONVERT.

The end-to-end indexing + content: search path is covered generically by
test_document_search.py (any FORMAT_CONVERT text output is indexed).

Run:
    python -m unittest ci.test_document_convert -v
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-document-convert-test-secret')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

from unittest.mock import patch  # noqa: E402
from worker.arcworker.tools import documents  # noqa: E402
from worker.arcworker.tools.documents import (  # noqa: E402
    _docx_xml_to_text,
    word_to_text,
)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx_bytes(document_xml: str) -> bytes:
    """Build a minimal .docx (ZIP) carrying the given word/document.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('[Content_Types].xml', '<Types/>')
        zf.writestr('word/document.xml', document_xml)
    return buf.getvalue()


def _write_tmp(data: bytes, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


class TestDocxToText(unittest.TestCase):

    def tearDown(self):
        for p in getattr(self, '_tmp', []):
            p.unlink(missing_ok=True)

    def _docx(self, document_xml: str) -> Path:
        p = _write_tmp(_docx_bytes(document_xml), '.docx')
        self._tmp = getattr(self, '_tmp', []) + [p]
        return p

    def test_paragraphs_tabs_and_breaks(self):
        xml = (
            f'<w:document {_W}><w:body>'
            '<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Col1</w:t><w:tab/><w:t>Col2</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>line1</w:t><w:br/><w:t>line2</w:t></w:r></w:p>'
            '</w:body></w:document>'
        )
        res = word_to_text(self._docx(xml))
        self.assertTrue(res['success'], res)
        self.assertEqual(res['tool'], 'docx')
        self.assertEqual(res['text'], 'Hello world\nCol1\tCol2\nline1\nline2')

    def test_empty_document(self):
        res = word_to_text(self._docx(f'<w:document {_W}><w:body/></w:document>'))
        self.assertTrue(res['success'], res)
        self.assertEqual(res['text'], '')

    def test_missing_document_xml(self):
        # A ZIP that is not a Word document (no word/document.xml).
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('hello.txt', 'not word')
        p = _write_tmp(buf.getvalue(), '.docx')
        self._tmp = getattr(self, '_tmp', []) + [p]
        res = word_to_text(p)
        self.assertFalse(res['success'])
        self.assertIn('document.xml', res['error'])

    def test_oversized_document_xml_rejected(self):
        # A member whose declared uncompressed size exceeds the cap is refused
        # up front (guarding against a zip bomb).  Patch the cap small rather
        # than build a 64 MiB payload.
        xml = f'<w:document {_W}><w:body>' + ('x' * 4096) + '</w:body></w:document>'
        p = self._docx(xml)
        with patch.object(documents, '_DOCX_XML_CAP', 64):
            res = word_to_text(p)
        self.assertFalse(res['success'])
        self.assertIn('too large', res['error'])

    def test_xml_linearisation_direct(self):
        xml = (
            f'<w:document {_W}><w:body>'
            '<w:p><w:r><w:t>a</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>b</w:t></w:r></w:p>'
            '</w:body></w:document>'
        ).encode()
        self.assertEqual(_docx_xml_to_text(xml), 'a\nb')


class TestDocPathGraceful(unittest.TestCase):

    def test_non_zip_doc_fails_without_crash(self):
        # A non-ZIP file routes to the legacy .doc path.  antiword/catdoc are
        # absent here (worker-image only) OR would reject this garbage, so the
        # result is a clean failure either way — never an exception.
        p = _write_tmp(b'\xd0\xcf\x11\xe0garbage-not-a-real-doc', '.doc')
        try:
            res = word_to_text(p)
            self.assertFalse(res['success'])
            self.assertIsInstance(res.get('error'), str)
        finally:
            p.unlink(missing_ok=True)


class TestWordDetectionWiring(unittest.TestCase):

    def test_extension_map_direct_upload(self):
        from arcology_shared.artefact_types import detect_artefact_type
        from arcology_shared.enums import ArtefactType
        self.assertEqual(detect_artefact_type('report.doc'), ArtefactType.MS_WORD)
        self.assertEqual(detect_artefact_type('report.docx'), ArtefactType.MS_WORD)
        self.assertEqual(detect_artefact_type('REPORT.DOC'), ArtefactType.MS_WORD)

    def test_viewable_type_extraction_scan(self):
        from arcology_shared.artefact_types import viewable_artefact_type
        from arcology_shared.enums import ArtefactType
        self.assertEqual(viewable_artefact_type('memo.doc', None), ArtefactType.MS_WORD)
        self.assertEqual(viewable_artefact_type('memo.docx', None), ArtefactType.MS_WORD)

    def test_classified_convertible(self):
        from arcology_shared.content_categories import ContentCategory, classify_content
        self.assertIn(ContentCategory.CONVERTIBLE, classify_content('memo.doc', None))

    def test_queues_format_convert(self):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.services.artefact_types import ANALYSIS_MAP
        self.assertEqual(ANALYSIS_MAP[ArtefactType.MS_WORD], [AnalysisType.FORMAT_CONVERT])


class TestPdfWiring(unittest.TestCase):

    def test_pdf_conversion_graceful_without_tool(self):
        from worker.arcworker.tools.documents import pdf_to_text
        # pdftotext lives only in the worker image (absent here), or would reject
        # this garbage — either way a clean failure, never an exception.
        p = _write_tmp(b'%PDF-1.4 not really a pdf', '.pdf')
        try:
            res = pdf_to_text(p)
            self.assertFalse(res['success'])
            self.assertIsInstance(res.get('error'), str)
        finally:
            p.unlink(missing_ok=True)

    def test_pdf_detection_and_wiring(self):
        from arcology_shared.artefact_types import (
            detect_artefact_type,
            viewable_artefact_type,
        )
        from arcology_shared.content_categories import ContentCategory, classify_content
        from arcology_shared.enums import ArtefactType
        self.assertEqual(detect_artefact_type('manual.pdf'), ArtefactType.PDF)
        self.assertEqual(viewable_artefact_type('manual.pdf', None), ArtefactType.PDF)
        self.assertIn(ContentCategory.CONVERTIBLE, classify_content('manual.pdf', None))

    def test_pdf_queues_format_convert(self):
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.services.artefact_types import ANALYSIS_MAP
        self.assertIn(AnalysisType.FORMAT_CONVERT, ANALYSIS_MAP[ArtefactType.PDF])


class TestExcelWiring(unittest.TestCase):

    def test_xls_conversion_graceful_without_tool(self):
        from worker.arcworker.tools.documents import xls_to_text
        p = _write_tmp(b'\xd0\xcf\x11\xe0not-a-real-xls', '.xls')
        try:
            res = xls_to_text(p)
            self.assertFalse(res['success'])
            self.assertIsInstance(res.get('error'), str)
        finally:
            p.unlink(missing_ok=True)

    def test_xls_detection_and_wiring(self):
        from arcology_shared.artefact_types import (
            detect_artefact_type,
            viewable_artefact_type,
        )
        from arcology_shared.content_categories import ContentCategory, classify_content
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.services.artefact_types import ANALYSIS_MAP
        self.assertEqual(detect_artefact_type('accounts.xls'), ArtefactType.MS_EXCEL)
        self.assertEqual(viewable_artefact_type('accounts.xls', None), ArtefactType.MS_EXCEL)
        self.assertIn(ContentCategory.CONVERTIBLE, classify_content('accounts.xls', None))
        self.assertEqual(ANALYSIS_MAP[ArtefactType.MS_EXCEL], [AnalysisType.FORMAT_CONVERT])


class TestPowerpointWiring(unittest.TestCase):

    def test_ppt_conversion_graceful_without_tool(self):
        from worker.arcworker.tools.documents import ppt_to_text
        p = _write_tmp(b'\xd0\xcf\x11\xe0not-a-real-ppt', '.ppt')
        try:
            res = ppt_to_text(p)
            self.assertFalse(res['success'])
            self.assertIsInstance(res.get('error'), str)
        finally:
            p.unlink(missing_ok=True)

    def test_ppt_detection_and_wiring(self):
        from arcology_shared.artefact_types import (
            detect_artefact_type,
            viewable_artefact_type,
        )
        from arcology_shared.content_categories import ContentCategory, classify_content
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.services.artefact_types import ANALYSIS_MAP
        self.assertEqual(detect_artefact_type('deck.ppt'), ArtefactType.MS_POWERPOINT)
        self.assertEqual(viewable_artefact_type('deck.ppt', None), ArtefactType.MS_POWERPOINT)
        self.assertIn(ContentCategory.CONVERTIBLE, classify_content('deck.ppt', None))
        self.assertEqual(ANALYSIS_MAP[ArtefactType.MS_POWERPOINT], [AnalysisType.FORMAT_CONVERT])


class TestRtfWiring(unittest.TestCase):

    def test_rtf_conversion_graceful_without_tool(self):
        from worker.arcworker.tools.documents import rtf_to_text
        p = _write_tmp(rb'{\rtf1 hello}', '.rtf')
        try:
            res = rtf_to_text(p)
            self.assertFalse(res['success'])
            self.assertIsInstance(res.get('error'), str)
        finally:
            p.unlink(missing_ok=True)

    def test_strip_unrtf_header(self):
        from worker.arcworker.tools.documents import _strip_unrtf_header
        raw = "### header line\n### fonts: 2\nActual body text\nmore text\n"
        self.assertEqual(_strip_unrtf_header(raw), "Actual body text\nmore text")

    def test_rtf_detection_and_wiring(self):
        from arcology_shared.artefact_types import (
            detect_artefact_type,
            viewable_artefact_type,
        )
        from arcology_shared.content_categories import ContentCategory, classify_content
        from arcology_shared.enums import AnalysisType, ArtefactType
        from myapp.services.artefact_types import ANALYSIS_MAP
        self.assertEqual(detect_artefact_type('letter.rtf'), ArtefactType.RTF)
        self.assertEqual(viewable_artefact_type('letter.rtf', None), ArtefactType.RTF)
        self.assertIn(ContentCategory.CONVERTIBLE, classify_content('letter.rtf', None))
        self.assertEqual(ANALYSIS_MAP[ArtefactType.RTF], [AnalysisType.FORMAT_CONVERT])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
