"""
Document → plain-text extraction (Track 1 of the document full-text plan).

Converts word-processor / DTP documents to UTF-8 plain text so their content
can be full-text indexed (search_documents) and shown in the text viewer.  Each
converter returns a standard :func:`tool_result` dict carrying the extracted
``text`` on success.

Currently supported:
  - Microsoft Word ``.doc``  (legacy OLE binary, Word 2-2003) via ``antiword``,
    falling back to ``catdoc``.
  - Microsoft Word ``.docx`` (OOXML) via the standard library.

See ``doc/plans/DOCUMENT_FULLTEXT_PLAN.md``.
"""

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from ..config import log
from .base import exception_result, run_tool_with_output, tool_result

# Bound on the decompressed ``word/document.xml`` we will parse from a .docx.
# A .docx is a ZIP, so an unbounded read of its markup would be a zip-bomb
# vector; this cap is far above any genuine Word document's markup size.
_DOCX_XML_CAP = 64 * 1024 * 1024

# WordprocessingML namespace (OOXML main part).
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

# Per-conversion subprocess timeout (antiword/catdoc are fast; this only guards
# against a pathological input wedging the tool).
_WORD_TOOL_TIMEOUT = 120


def word_to_text(path: Path) -> dict:
    """Extract plain text from a Microsoft Word document (.doc or .docx).

    Dispatches on the container rather than the extension: a .docx is a ZIP
    (OOXML) parsed in-process; anything else is treated as a legacy binary
    .doc and handed to antiword / catdoc.  Returns
    ``tool_result(success, tool=..., text=...)``.
    """
    if zipfile.is_zipfile(path):
        return _docx_to_text(path)
    return _doc_to_text(path)


def _doc_to_text(path: Path) -> dict:
    """Legacy binary .doc → text via antiword (catdoc fallback).

    ``antiword -w 0`` disables line wrapping; ``-m UTF-8.txt`` selects the
    UTF-8 output mapping.  If antiword is absent or fails, catdoc is tried.
    """
    attempts = (
        ['antiword', '-w', '0', '-m', 'UTF-8.txt', str(path)],
        ['catdoc', '-d', 'utf-8', str(path)],
    )
    last_output = None
    for cmd in attempts:
        try:
            result, output = run_tool_with_output(cmd, timeout=_WORD_TOOL_TIMEOUT)
        except FileNotFoundError:
            # Tool not installed — fall through to the next candidate.
            log.debug("%s not available for .doc conversion", cmd[0])
            continue
        last_output = output
        if result.returncode == 0:
            text = result.stdout.decode('utf-8', errors='replace')
            return tool_result(True, tool=cmd[0], text=text, process_output=output)
    return tool_result(
        False, tool='antiword',
        error='Word .doc conversion failed (antiword/catdoc unavailable or errored)',
        process_output=last_output,
    )


def _docx_to_text(path: Path) -> dict:
    """OOXML .docx → text using the standard library (no external tool)."""
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                info = zf.getinfo('word/document.xml')
            except KeyError:
                return tool_result(
                    False, tool='docx',
                    error='Not a Word .docx (no word/document.xml)')
            if info.file_size > _DOCX_XML_CAP:
                return tool_result(
                    False, tool='docx',
                    error=f'word/document.xml too large ({info.file_size} bytes)')
            with zf.open('word/document.xml') as f:
                # Read one byte past the cap so a member whose header understated
                # its size is still caught rather than silently truncated.
                xml_bytes = f.read(_DOCX_XML_CAP + 1)
        if len(xml_bytes) > _DOCX_XML_CAP:
            return tool_result(False, tool='docx',
                               error='word/document.xml exceeds size cap')
        return tool_result(True, tool='docx', text=_docx_xml_to_text(xml_bytes))
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return exception_result('docx', '.docx extraction failed')


def _docx_xml_to_text(xml_bytes: bytes) -> str:
    """Linearise WordprocessingML markup to plain text.

    One line per paragraph (``w:p``); text comes from ``w:t`` runs, with
    ``w:tab`` → tab and ``w:br`` / ``w:cr`` → newline.  Table cells contain
    their own paragraphs and so are captured in document order (structure is
    dropped — linear text is all FTS and the text viewer need).
    """
    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for p in root.iter(f'{_W}p'):
        buf: list[str] = []
        for node in p.iter():
            tag = node.tag
            if tag == f'{_W}t':
                buf.append(node.text or '')
            elif tag == f'{_W}tab':
                buf.append('\t')
            elif tag in (f'{_W}br', f'{_W}cr'):
                buf.append('\n')
        paragraphs.append(''.join(buf))
    return '\n'.join(paragraphs).strip('\n')

# vim: ts=4 sw=4 et
