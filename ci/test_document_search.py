"""
Document full-text search (the ``content:`` key + search_documents).

Covers, on the SQLite (ILIKE fallback) path — which exercises the indexing
handler and the security-critical gating logic that are dialect-independent:
  - handle_search_documents indexes FORMAT_CONVERT text outputs (direct artefact
    and extraction-scan, keyed by source_file), and is idempotent / scoped.
  - content: matches document text and returns a documents bucket.
  - Visibility: a private item's document is hidden from an anonymous user.
  - Restriction gate: a download-restricted artefact's document is withheld
    entirely from content results (the match is not even revealed).

The PostgreSQL FTS ranking/snippet path is validated separately against a real
PostgreSQL; here the ILIKE fallback + Python gate are what run in CI.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_document_search -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-document-search-test-secret')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _mk_analysis(db, artefact_id):
    from arcology_shared.enums import AnalysisType
    from myapp.database import Analysis, AnalysisStatus
    an = Analysis(
        artefact_id=artefact_id,
        analysis_type=AnalysisType.FORMAT_CONVERT,
        status=AnalysisStatus.COMPLETED,
        success=True,
    )
    db.session.add(an)
    db.session.flush()
    return an


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['PUBLIC_MODE'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from myapp.database import Analysis, Artefact, Item, SearchDocument
        self.db.session.rollback()
        for model in (SearchDocument, Analysis, Artefact, Item):
            model.query.delete()
        self.db.session.commit()
        self.ctx.pop()

    def _item(self, name='Doc Item', private=False):
        from myapp.database import Item
        it = Item(name=name)
        # private_effective is the denormalised flag the visibility clause reads.
        it.is_private = private
        it.private_effective = private
        self.db.session.add(it)
        self.db.session.flush()
        return it

    def _artefact(self, item, label='doc'):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact
        a = Artefact(item_id=item.id, label=label, artefact_type=ArtefactType.ACORN_TEXT,
                     original_filename=label, storage_path=label)
        self.db.session.add(a)
        self.db.session.flush()
        return a


class TestDocumentIndexing(_Base):

    def test_indexes_direct_and_extracted_text(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'text': 'direct artefact body'},                       # direct: no source_file
            {'type': 'text', 'text': 'inner file body', 'source_file': 'docs/note'},  # extracted
            {'type': 'image', 'filename': 'x.png'},                                 # ignored (not text)
            {'type': 'text', 'source_file': 'empty'},                               # ignored (no text)
        ]})
        self.db.session.commit()
        docs = SearchDocument.query.filter_by(artefact_id=a.id).all()
        self.assertEqual(len(docs), 2)
        by_path = {d.file_path: d.content for d in docs}
        self.assertEqual(by_path[None], 'direct artefact body')
        self.assertEqual(by_path['docs/note'], 'inner file body')

    def test_reindex_is_scoped_and_idempotent(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'text': 'v1', 'source_file': 'a'},
            {'type': 'text', 'text': 'keep me', 'source_file': 'b'},
        ]})
        self.db.session.commit()
        # Re-run touching only path 'a' — must replace 'a', leave 'b' intact.
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'text': 'v2', 'source_file': 'a'},
        ]})
        self.db.session.commit()
        docs = {d.file_path: d.content for d in SearchDocument.query.filter_by(artefact_id=a.id)}
        self.assertEqual(docs, {'a': 'v2', 'b': 'keep me'})

    def test_truncated_flag_stored(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'text': 'abc', 'text_truncated': True},
        ]})
        self.db.session.commit()
        self.assertTrue(SearchDocument.query.filter_by(artefact_id=a.id).one().truncated)


class TestStorageFallbackIndexing(_Base):
    """A FORMAT_CONVERT output with no inlined ``text`` (predates the payload)
    is indexed by reading the saved .txt back from storage — so
    rebuild-search-index covers historical text/Word outputs."""

    def setUp(self):
        super().setUp()
        import tempfile
        from arcology_shared.storage import LocalStorage
        self._tmpdir = tempfile.mkdtemp()
        self._prev_storage = self.app.storage
        self.app.storage = LocalStorage(
            uploads_dir=os.path.join(self._tmpdir, 'u'),
            outputs_dir=os.path.join(self._tmpdir, 'o'))

    def tearDown(self):
        import shutil
        self.app.storage = self._prev_storage
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()

    def _put_output(self, filename, data: bytes):
        from pathlib import Path
        src = Path(self._tmpdir) / 'src'
        src.write_bytes(data)
        key = self.app.storage.storage_key('outputs', filename)
        self.app.storage.put(key, src)
        src.unlink()

    def test_reads_text_from_storage_when_not_inlined(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        self._put_output('doc_0_text.txt', b'quick brown fox from storage')
        # Output as an old analysis stored it: filename only, no 'text'.
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'filename': 'doc_0_text.txt', 'source_file': 'Report'},
        ]})
        self.db.session.commit()
        doc = SearchDocument.query.filter_by(artefact_id=a.id).one()
        self.assertEqual(doc.file_path, 'Report')
        self.assertEqual(doc.content, 'quick brown fox from storage')
        self.assertFalse(doc.truncated)

    def test_inlined_text_wins_over_storage(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        self._put_output('doc_0_text.txt', b'STORAGE COPY')
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'text': 'INLINE COPY', 'filename': 'doc_0_text.txt'},
        ]})
        self.db.session.commit()
        self.assertEqual(
            SearchDocument.query.filter_by(artefact_id=a.id).one().content, 'INLINE COPY')

    def test_storage_read_capped_and_flagged(self):
        from unittest.mock import patch
        from myapp.database import SearchDocument
        from myapp.services import search_index
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        self._put_output('doc_0_text.txt', b'abcdefghij')
        with patch.object(search_index, '_INDEX_TEXT_CAP', 4):
            handle_search_documents(an, {'outputs': [
                {'type': 'text', 'filename': 'doc_0_text.txt'},
            ]})
        self.db.session.commit()
        doc = SearchDocument.query.filter_by(artefact_id=a.id).one()
        self.assertEqual(doc.content, 'abcd')
        self.assertTrue(doc.truncated)

    def test_missing_output_leaves_existing_row_untouched(self):
        from myapp.database import SearchDocument
        from myapp.services.search_index import handle_search_documents
        it = self._item()
        a = self._artefact(it)
        an = _mk_analysis(self.db, a.id)
        # Seed an existing indexed row for source_file 'Report'.
        self.db.session.add(SearchDocument(
            artefact_id=a.id, file_path='Report', content='previously indexed'))
        self.db.session.commit()
        # Re-run where the output file is gone from storage: the read fails, so
        # the row must be neither deleted nor replaced.
        handle_search_documents(an, {'outputs': [
            {'type': 'text', 'filename': 'gone.txt', 'source_file': 'Report'},
        ]})
        self.db.session.commit()
        doc = SearchDocument.query.filter_by(artefact_id=a.id).one()
        self.assertEqual(doc.content, 'previously indexed')


class TestContentSearch(_Base):

    def _index(self, artefact, text, source_file=None):
        from myapp.services.search_index import handle_search_documents
        an = _mk_analysis(self.db, artefact.id)
        out = {'type': 'text', 'text': text}
        if source_file is not None:
            out['source_file'] = source_file
        handle_search_documents(an, {'outputs': [out]})
        self.db.session.commit()

    def _search(self, query):
        from myapp.services.search import _run_search, parse_query
        with self.app.test_request_context(f'/search/?q={query}'):
            return _run_search(parse_query(query), page=1, per_page=50)

    def test_content_match_returns_document_bucket(self):
        it = self._item()
        a = self._artefact(it, 'story')
        self._index(a, 'once upon a midnight dreary while I pondered weak')
        res = self._search('content:midnight')
        self.assertEqual(res['totals']['documents'], 1)
        self.assertEqual(res['documents'][0]['artefact'].id, a.id)

    def test_content_no_match(self):
        it = self._item()
        self._index(self._artefact(it), 'hello world')
        self.assertEqual(self._search('content:zzzznope')['totals']['documents'], 0)

    def test_private_item_document_hidden_from_anonymous(self):
        it = self._item('Secret', private=True)
        self._index(self._artefact(it), 'clandestine content here')
        # Anonymous (PUBLIC_MODE) must not see a private item's document.
        self.assertEqual(self._search('content:clandestine')['totals']['documents'], 0)

    def test_restricted_artefact_document_withheld(self):
        from myapp.database import ArtefactRestriction
        from myapp.enums import RestrictionType
        it = self._item()
        a = self._artefact(it, 'malware sample')
        self._index(a, 'this document contains the secret payload string')
        self.db.session.add(ArtefactRestriction(
            artefact_id=a.id, restriction_type=RestrictionType.MALWARE))
        self.db.session.commit()
        res = self._search('content:payload')
        # The match is withheld entirely for a user who can't bypass the
        # restriction — the row is dropped, not just its snippet.
        self.assertEqual(res['documents'], [])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
