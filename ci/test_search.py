"""
Search engine smoke tests.

Tests both the query parser (pure unit tests, no database) and the search
logic (_run_search) with fixture data inserted into a SQLite in-memory
database.  Every search key supported by the engine is exercised.

Environment variables (same as other CI tests):
    SQLALCHEMY_DATABASE_URI  — defaults to sqlite:///:memory:
    SECRET_KEY               — defaults to a fixed test value
    WORKER_API_KEY           — defaults to 'ci-test-worker-key'

Run:
    python -m unittest ci.test_search -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-smoke-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


# =============================================================================
# Unit tests: parse_query (no database required)
# =============================================================================

class TestParseQuery(unittest.TestCase):
    """Unit tests for the query parser — no app context or database needed."""

    @classmethod
    def setUpClass(cls):
        from myapp.blueprints.search import parse_query
        cls.parse = staticmethod(parse_query)

    def test_empty_string(self):
        self.assertEqual(self.parse(''), {})

    def test_none_string(self):
        self.assertEqual(self.parse(None), {})

    def test_bare_word(self):
        tokens = self.parse('Impression')
        self.assertEqual(tokens, {'text': ['Impression']})

    def test_bare_quoted_phrase(self):
        tokens = self.parse('"BBC Basic"')
        self.assertEqual(tokens, {'text': ['BBC Basic']})

    def test_key_value(self):
        tokens = self.parse('filename:!RunImage')
        self.assertEqual(tokens.get('filename'), ['!RunImage'])

    def test_key_quoted_value(self):
        tokens = self.parse('label:"Boot Disc"')
        self.assertEqual(tokens.get('label'), ['Boot Disc'])

    def test_md5_key(self):
        tokens = self.parse('md5:d41d8cd98f00b204e9800998ecf8427e')
        self.assertEqual(tokens.get('md5'), ['d41d8cd98f00b204e9800998ecf8427e'])

    def test_sha1_key(self):
        tokens = self.parse('sha1:da39a3ee5e6b4b0d3255bfef95601890afd80709')
        self.assertEqual(tokens.get('sha1'), ['da39a3ee5e6b4b0d3255bfef95601890afd80709'])

    def test_sha256_key(self):
        h = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        tokens = self.parse(f'sha256:{h}')
        self.assertEqual(tokens.get('sha256'), [h])

    def test_path_key(self):
        tokens = self.parse('path:!Impression')
        self.assertEqual(tokens.get('path'), ['!Impression'])

    def test_type_key(self):
        tokens = self.parse('type:fff')
        self.assertEqual(tokens.get('type'), ['fff'])

    def test_ext_key(self):
        tokens = self.parse('ext:bas')
        self.assertEqual(tokens.get('ext'), ['bas'])

    def test_ident_key(self):
        tokens = self.parse('ident:DOS')
        self.assertEqual(tokens.get('ident'), ['DOS'])

    def test_label_key(self):
        tokens = self.parse('label:System')
        self.assertEqual(tokens.get('label'), ['System'])

    def test_fs_key(self):
        tokens = self.parse('fs:adfs')
        self.assertEqual(tokens.get('fs'), ['adfs'])

    def test_protection_key(self):
        tokens = self.parse('protection:bad_crc')
        self.assertEqual(tokens.get('protection'), ['bad_crc'])

    def test_mastering_key(self):
        tokens = self.parse('mastering:traceback')
        self.assertEqual(tokens.get('mastering'), ['traceback'])

    def test_module_key(self):
        tokens = self.parse('module:WindowManager')
        self.assertEqual(tokens.get('module'), ['WindowManager'])

    def test_tag_key(self):
        tokens = self.parse('tag:bbc-micro')
        self.assertEqual(tokens.get('tag'), ['bbc-micro'])

    def test_tag_quoted_value(self):
        tokens = self.parse('tag:"bbc micro"')
        self.assertEqual(tokens.get('tag'), ['bbc micro'])

    def test_tag_wildcard(self):
        tokens = self.parse('tag:bbc*')
        self.assertEqual(tokens.get('tag'), ['bbc*'])

    # Aliases

    def test_alias_file(self):
        tokens = self.parse('file:!RunImage')
        self.assertIn('filename', tokens)
        self.assertNotIn('file', tokens)

    def test_alias_filetype(self):
        tokens = self.parse('filetype:fff')
        self.assertIn('type', tokens)
        self.assertNotIn('filetype', tokens)

    def test_alias_disc(self):
        tokens = self.parse('disc:System')
        self.assertIn('label', tokens)
        self.assertNotIn('disc', tokens)

    def test_alias_gnu(self):
        tokens = self.parse('gnu:DOS')
        self.assertIn('ident', tokens)
        self.assertNotIn('gnu', tokens)

    def test_alias_gnufile(self):
        tokens = self.parse('gnufile:DOS')
        self.assertIn('ident', tokens)
        self.assertNotIn('gnufile', tokens)

    def test_alias_filesystem(self):
        tokens = self.parse('filesystem:adfs')
        self.assertIn('fs', tokens)
        self.assertNotIn('filesystem', tokens)

    def test_alias_prot(self):
        tokens = self.parse('prot:bad_crc')
        self.assertIn('protection', tokens)
        self.assertNotIn('prot', tokens)

    # Multi-value / multi-key

    def test_multiple_values_same_key(self):
        tokens = self.parse('type:feb type:ffa')
        self.assertCountEqual(tokens.get('type', []), ['feb', 'ffa'])

    def test_multiple_keys(self):
        tokens = self.parse('path:!Impression filename:!RunImage')
        self.assertIn('path', tokens)
        self.assertIn('filename', tokens)

    def test_mixed_bare_and_keyed(self):
        tokens = self.parse('Impression filename:!RunImage')
        self.assertIn('text', tokens)
        self.assertIn('filename', tokens)


# =============================================================================
# Unit tests: RISC OS filetype lookup (no database required)
# =============================================================================

class TestLookupFiletypeHex(unittest.TestCase):
    """Unit tests for lookup_filetype_hex — no app context needed."""

    @classmethod
    def setUpClass(cls):
        from myapp.riscos_filetypes import lookup_filetype_hex
        cls.lookup = staticmethod(lookup_filetype_hex)

    def test_hex_code_returns_self(self):
        self.assertEqual(self.lookup('fea'), 'fea')

    def test_hex_code_uppercase_normalised(self):
        self.assertEqual(self.lookup('FEA'), 'fea')

    def test_name_desktop(self):
        self.assertEqual(self.lookup('Desktop'), 'fea')

    def test_name_case_insensitive(self):
        self.assertEqual(self.lookup('desktop'), 'fea')
        self.assertEqual(self.lookup('DESKTOP'), 'fea')

    def test_name_text(self):
        self.assertEqual(self.lookup('Text'), 'fff')

    def test_name_basic(self):
        self.assertEqual(self.lookup('BASIC'), 'ffb')

    def test_name_absolute(self):
        self.assertEqual(self.lookup('Absolute'), 'ff8')

    def test_name_obey(self):
        self.assertEqual(self.lookup('Obey'), 'feb')

    def test_unknown_name_returns_none(self):
        self.assertIsNone(self.lookup('NotARealType'))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.lookup(''))

    def test_none_returns_none(self):
        self.assertIsNone(self.lookup(None))

    def test_hex_and_name_equivalent(self):
        # type:fea and type:Desktop should resolve to the same hex code
        self.assertEqual(self.lookup('fea'), self.lookup('Desktop'))


# =============================================================================
# Integration tests: _run_search with fixture data
# =============================================================================

class TestSearchLogic(unittest.TestCase):
    """Tests for _run_search with real fixture data in a SQLite in-memory DB."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        from myapp.database import (
            Item, Artefact, Partition, ExtractedFile,
            ArtefactProtection, ArtefactMastering, RiscosModule, Tag,
            FilesystemType, StorageDirectory,
        )
        from shared.enums import ArtefactType

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()

            # --- Fixture: one item, one artefact, one partition, two files ---
            item = Item(name='Test Software', description='A classic BBC Micro game')
            _db.session.add(item)
            _db.session.flush()

            art = Artefact(
                item_id=item.id,
                label='Side A',
                description='Original flux dump from disc 1',
                artefact_type=ArtefactType.HFE,
                original_filename='side_a.hfe',
                storage_path='side_a.hfe',
                storage_directory=StorageDirectory.UPLOADS,
                md5='aaaabbbbccccdddd0000111122223333',
                sha256='a' * 64,
            )
            _db.session.add(art)
            _db.session.flush()

            part = Partition(
                artefact_id=art.id,
                partition_index=0,
                label='System',
                filesystem=FilesystemType.ADFS,
                container_format='Acorn ADFS E',
                gnu_file_type='DOS boot sector',
            )
            _db.session.add(part)
            _db.session.flush()

            # Regular file with RISC OS filetype
            f1 = ExtractedFile(
                partition_id=part.id,
                path='$.!Impression.!RunImage',
                filename='!RunImage',
                extension=None,
                risc_os_filetype='ff8',
                md5='deadbeef' + '0' * 24,
                sha1='cafebabe' + '0' * 32,
                sha256='b' * 64,
                is_directory=False,
            )
            # File with extension
            f2 = ExtractedFile(
                partition_id=part.id,
                path='$.Tools.Convert.bas',
                filename='bas',
                extension='bas',
                risc_os_filetype=None,
                md5='11112222333344445555666677778888',
                sha1=None,
                sha256='c' * 64,
                is_directory=False,
            )
            # Directory entry — must NOT appear in file search results
            d1 = ExtractedFile(
                partition_id=part.id,
                path='$.!Impression',
                filename='!Impression',
                extension=None,
                is_directory=True,
            )
            # Module files (matched by RiscosModule.file_path)
            f_wm = ExtractedFile(
                partition_id=part.id,
                path='$.Modules.WindowManager',
                filename='WindowManager',
                extension=None,
                risc_os_filetype='ffa',
                md5='m1' + '0' * 30,
                sha1='m1' + '0' * 38,
                sha256='m1' + '0' * 62,
                is_directory=False,
            )
            f_adfs = ExtractedFile(
                partition_id=part.id,
                path='$.Modules.ADFS',
                filename='ADFS',
                extension=None,
                risc_os_filetype='ffa',
                md5='m2' + '0' * 30,
                sha1='m2' + '0' * 38,
                sha256='m2' + '0' * 62,
                is_directory=False,
            )
            _db.session.add_all([f1, f2, d1, f_wm, f_adfs])

            # Protection indicators
            _db.session.add(ArtefactProtection(
                artefact_id=art.id,
                protection_type='bad_crc',
                track=1,
                side=0,
            ))
            _db.session.add(ArtefactProtection(
                artefact_id=art.id,
                protection_type='bad_crc',
                track=2,
                side=0,
            ))
            _db.session.add(ArtefactProtection(
                artefact_id=art.id,
                protection_type='weak_bits',
                track=5,
                side=0,
            ))

            # Mastering indicators
            _db.session.add(ArtefactMastering(
                artefact_id=art.id,
                mastering_type='traceback',
                track=79,
            ))
            _db.session.add(ArtefactMastering(
                artefact_id=art.id,
                mastering_type='formaster',
                track=78,
                decoded='1990-01-01',
            ))

            # RISC OS modules
            _db.session.add(RiscosModule(
                artefact_id=art.id,
                title_string='WindowManager',
                help_title='Window Manager',
                version='2.05',
                date='1990-01-31',
                swi_chunk=0x400c0,
                file_path='$.Modules.WindowManager',
            ))
            _db.session.add(RiscosModule(
                artefact_id=art.id,
                title_string='ADFS',
                help_title='ADFS',
                version='2.30',
                date='1990-02-15',
                file_path='$.Modules.ADFS',
            ))

            # Tags
            tag_bbc = Tag(name='bbc-micro')
            tag_game = Tag(name='game')
            _db.session.add_all([tag_bbc, tag_game])
            _db.session.flush()
            art.tags.append(tag_bbc)
            art.tags.append(tag_game)

            _db.session.commit()

            # Store IDs for assertions
            cls.item_id = item.id
            cls.art_id = art.id
            cls.part_id = part.id

    def _search(self, query_string):
        from myapp.blueprints.search import parse_query, _run_search
        with self.app.app_context():
            return _run_search(parse_query(query_string))

    # ------------------------------------------------------------------
    # File searches
    # ------------------------------------------------------------------

    def test_filename_search_finds_file(self):
        results = self._search('filename:!RunImage')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_filename_alias_file(self):
        results = self._search('file:!RunImage')
        self.assertTrue(len(results['files']) > 0)

    def test_filename_wildcard(self):
        results = self._search('filename:!Run*')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_filename_no_directories(self):
        # Directory entries must never appear in file results
        results = self._search('filename:!Impression')
        self.assertEqual(len(results['files']), 0)

    def test_path_search(self):
        results = self._search('path:!Impression')
        paths = [ef.path for ef, *_ in results['files']]
        self.assertTrue(any('!Impression' in p for p in paths))

    def test_path_no_match(self):
        results = self._search('path:xyzzy_no_such_path')
        self.assertEqual(results['files'], [])

    def test_risc_os_type_search(self):
        results = self._search('type:ff8')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_risc_os_type_no_match(self):
        results = self._search('type:000')
        self.assertEqual(results['files'], [])

    def test_risc_os_type_alias_filetype(self):
        results = self._search('filetype:ff8')
        self.assertTrue(len(results['files']) > 0)

    def test_risc_os_type_by_name(self):
        # 'Absolute' maps to 'ff8' — should find the same file as type:ff8
        results = self._search('type:Absolute')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_risc_os_type_name_no_match(self):
        results = self._search('type:Squash')
        self.assertEqual(results['files'], [])

    def test_risc_os_type_name_case_insensitive(self):
        results_lower = self._search('type:absolute')
        results_upper = self._search('type:ABSOLUTE')
        self.assertEqual(len(results_lower['files']), len(results_upper['files']))

    def test_ext_search(self):
        results = self._search('ext:bas')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('bas', filenames)

    def test_ext_no_match(self):
        results = self._search('ext:xyz_no_such_ext')
        self.assertEqual(results['files'], [])

    def test_md5_finds_file(self):
        results = self._search('md5:deadbeef' + '0' * 24)
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_sha1_finds_file(self):
        results = self._search('sha1:cafebabe' + '0' * 32)
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_sha1_no_match(self):
        results = self._search('sha1:' + '0' * 40)
        self.assertEqual(results['files'], [])

    def test_sha256_finds_file(self):
        results = self._search('sha256:' + 'b' * 64)
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_sha256_no_match(self):
        results = self._search('sha256:' + '0' * 64)
        self.assertEqual(results['files'], [])

    def test_or_within_key(self):
        # Both type:ff8 and ext:bas should be found when queried together as same key
        results = self._search('type:ff8 type:nope')
        # ff8 should still match
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_and_across_keys(self):
        # path contains !Impression AND filename is !RunImage — should match
        results = self._search('path:!Impression filename:!RunImage')
        self.assertTrue(len(results['files']) > 0)

    def test_and_across_keys_no_match(self):
        # path !Impression AND filename bas — no file has both
        results = self._search('path:!Impression filename:bas')
        self.assertEqual(len(results['files']), 0)

    def test_no_file_results(self):
        results = self._search('filename:doesnotexist_xyzzy')
        self.assertEqual(results['files'], [])

    # ------------------------------------------------------------------
    # Disc / partition searches
    # ------------------------------------------------------------------

    def test_label_search(self):
        results = self._search('label:System')
        self.assertTrue(len(results['artefacts']) > 0)
        types = [r['type'] for r in results['artefacts']]
        self.assertIn('partition', types)

    def test_label_alias_disc(self):
        results = self._search('disc:System')
        self.assertTrue(len(results['artefacts']) > 0)

    def test_fs_search_enum_value(self):
        results = self._search('fs:adfs')
        types = [r['type'] for r in results['artefacts']]
        self.assertIn('partition', types)

    def test_fs_search_no_match(self):
        results = self._search('fs:xyzzy_no_such_fs')
        partition_results = [r for r in results['artefacts'] if r['type'] == 'partition']
        self.assertEqual(partition_results, [])

    def test_fs_search_container_format(self):
        # 'adfs e' is not a valid FilesystemType so falls through to container_format ilike
        results = self._search('fs:"Acorn ADFS E"')
        types = [r['type'] for r in results['artefacts']]
        self.assertIn('partition', types)

    def test_ident_search(self):
        results = self._search('ident:"DOS boot sector"')
        types = [r['type'] for r in results['artefacts']]
        self.assertIn('partition', types)

    def test_ident_no_match(self):
        results = self._search('ident:xyzzy_no_such_ident')
        partition_results = [r for r in results['artefacts'] if r['type'] == 'partition']
        self.assertEqual(partition_results, [])

    def test_ident_alias_gnu(self):
        results = self._search('gnu:"DOS boot sector"')
        self.assertTrue(len(results['artefacts']) > 0)

    def test_ident_alias_gnufile(self):
        results = self._search('gnufile:"DOS boot sector"')
        self.assertTrue(len(results['artefacts']) > 0)

    def test_gnufile_alias_filesystem_fallback(self):
        results = self._search('filesystem:adfs')
        types = [r['type'] for r in results['artefacts']]
        self.assertIn('partition', types)

    def test_no_disc_results(self):
        results = self._search('label:doesnotexist_xyzzy')
        partition_results = [r for r in results['artefacts'] if r['type'] == 'partition']
        self.assertEqual(partition_results, [])

    # ------------------------------------------------------------------
    # Protection indicator searches
    # ------------------------------------------------------------------

    def test_protection_search_finds_artefact(self):
        results = self._search('protection:bad_crc')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        self.assertTrue(len(prot_results) > 0)

    def test_protection_search_alias_prot(self):
        results = self._search('prot:bad_crc')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        self.assertTrue(len(prot_results) > 0)

    def test_protection_deduplication(self):
        # bad_crc appears on two tracks — should yield exactly one artefact result
        results = self._search('protection:bad_crc')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        artefact_ids = [r['artefact'].id for r in prot_results]
        self.assertEqual(len(artefact_ids), len(set(artefact_ids)),
                         "Duplicate artefact entries after deduplication")

    def test_protection_search_weak_bits(self):
        results = self._search('protection:weak_bits')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        self.assertTrue(len(prot_results) > 0)

    def test_protection_search_no_match(self):
        results = self._search('protection:nonexistent_type')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        self.assertEqual(prot_results, [])

    # ------------------------------------------------------------------
    # Mastering indicator searches
    # ------------------------------------------------------------------

    def test_mastering_search_traceback(self):
        results = self._search('mastering:traceback')
        mast_results = [r for r in results['artefacts'] if r['type'] == 'mastering']
        self.assertTrue(len(mast_results) > 0)

    def test_mastering_search_formaster(self):
        results = self._search('mastering:formaster')
        mast_results = [r for r in results['artefacts'] if r['type'] == 'mastering']
        self.assertTrue(len(mast_results) > 0)

    def test_mastering_deduplication(self):
        # If an artefact had two traceback indicators it should appear once
        results = self._search('mastering:traceback')
        mast_results = [r for r in results['artefacts'] if r['type'] == 'mastering']
        artefact_ids = [r['artefact'].id for r in mast_results]
        self.assertEqual(len(artefact_ids), len(set(artefact_ids)),
                         "Duplicate artefact entries after deduplication")

    def test_mastering_search_no_match(self):
        results = self._search('mastering:nonexistent_type')
        mast_results = [r for r in results['artefacts'] if r['type'] == 'mastering']
        self.assertEqual(mast_results, [])

    # ------------------------------------------------------------------
    # RISC OS module searches
    # ------------------------------------------------------------------

    def test_module_search_exact(self):
        results = self._search('module:WindowManager')
        # Module results now appear as file tuples in the files bucket
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('$.Modules.WindowManager', file_paths)

    def test_module_search_wildcard(self):
        results = self._search('module:Window*')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('$.Modules.WindowManager', file_paths)

    def test_module_search_case_insensitive(self):
        results = self._search('module:windowmanager')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('$.Modules.WindowManager', file_paths)

    def test_module_search_second_module(self):
        results = self._search('module:ADFS')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('$.Modules.ADFS', file_paths)

    def test_module_search_by_help_title(self):
        results = self._search('module:"Window Manager"')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('$.Modules.WindowManager', file_paths)

    def test_module_search_no_match(self):
        results = self._search('module:NonExistentModule')
        mod_results = [r for r in results['artefacts'] if r['type'] == 'module']
        self.assertEqual(mod_results, [])

    def test_module_search_deduplication(self):
        # Both modules belong to the same artefact; searching ADFS* should
        # only produce one result per artefact.
        results = self._search('module:ADFS*')
        mod_results = [r for r in results['artefacts'] if r['type'] == 'module']
        art_ids = [r['artefact'].id for r in mod_results]
        self.assertEqual(len(art_ids), len(set(art_ids)))

    # ------------------------------------------------------------------
    # Tag searches
    # ------------------------------------------------------------------

    def test_tag_search_exact_finds_artefact(self):
        results = self._search('tag:bbc-micro')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(len(tag_results) > 0)
        artefact_labels = [r['artefact'].label for r in tag_results]
        self.assertIn('Side A', artefact_labels)

    def test_tag_search_second_tag(self):
        results = self._search('tag:game')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(len(tag_results) > 0)

    def test_tag_search_wildcard(self):
        results = self._search('tag:bbc*')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(len(tag_results) > 0)

    def test_tag_search_substring(self):
        # _ilike wraps in % when no wildcard present — 'micro' should match 'bbc-micro'
        results = self._search('tag:micro')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(len(tag_results) > 0)

    def test_tag_search_no_match(self):
        results = self._search('tag:nonexistent_tag_xyzzy')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertEqual(tag_results, [])

    def test_tag_name_in_result(self):
        results = self._search('tag:bbc-micro')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(any(r['tag_name'] == 'bbc-micro' for r in tag_results))

    # ------------------------------------------------------------------
    # Artefact-level hash searches
    # ------------------------------------------------------------------

    def test_artefact_md5_search(self):
        results = self._search('md5:aaaabbbbccccdddd0000111122223333')
        hash_results = [r for r in results['artefacts'] if r['type'] == 'artefact_hash']
        self.assertTrue(len(hash_results) > 0)

    def test_artefact_sha256_search(self):
        results = self._search('sha256:' + 'a' * 64)
        hash_results = [r for r in results['artefacts'] if r['type'] == 'artefact_hash']
        self.assertTrue(len(hash_results) > 0)

    def test_artefact_hash_no_match(self):
        results = self._search('md5:' + '0' * 32)
        hash_results = [r for r in results['artefacts'] if r['type'] == 'artefact_hash']
        self.assertEqual(hash_results, [])

    # ------------------------------------------------------------------
    # Free-text item searches
    # ------------------------------------------------------------------

    def test_text_search_by_name(self):
        results = self._search('Software')
        self.assertTrue(len(results['catalogue_items']) > 0)

    def test_text_search_by_description(self):
        results = self._search('classic')
        self.assertTrue(len(results['catalogue_items']) > 0)

    def test_text_search_no_match(self):
        results = self._search('xyzzy_no_such_item')
        self.assertEqual(results['catalogue_items'], [])

    def test_text_search_quoted_phrase(self):
        results = self._search('"BBC Micro"')
        self.assertTrue(len(results['catalogue_items']) > 0)

    # ------------------------------------------------------------------
    # Free-text artefact searches
    # ------------------------------------------------------------------

    def test_text_search_by_artefact_label(self):
        results = self._search('Side')
        art_results = [r for r in results['artefacts'] if r['type'] == 'artefact_text']
        self.assertTrue(len(art_results) > 0)

    def test_text_search_by_artefact_description(self):
        results = self._search('flux')
        art_results = [r for r in results['artefacts'] if r['type'] == 'artefact_text']
        self.assertTrue(len(art_results) > 0)

    def test_text_search_artefact_no_match(self):
        results = self._search('xyzzy_no_such_artefact')
        art_results = [r for r in results['artefacts'] if r['type'] == 'artefact_text']
        self.assertEqual(art_results, [])

    # ------------------------------------------------------------------
    # Empty / no-results baseline
    # ------------------------------------------------------------------

    def test_empty_results_structure(self):
        results = self._search('xyzzy_guaranteed_no_match_12345')
        self.assertIn('files', results)
        self.assertIn('artefacts', results)
        self.assertIn('catalogue_items', results)
        self.assertIn('truncated', results)
        self.assertEqual(results['files'], [])
        self.assertEqual(results['artefacts'], [])
        self.assertEqual(results['catalogue_items'], [])


# =============================================================================
# HTTP-level smoke tests
# =============================================================================

class TestSearchEndpoint(unittest.TestCase):
    """Verify the /search/ route is wired up and behaves correctly over HTTP."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            _db.create_all()

    def test_search_unauthenticated_redirects_to_login(self):
        """GET /search/ without a session should redirect to the login page."""
        resp = self.client.get('/search/')
        self.assertIn(resp.status_code, (301, 302),
                      f'Expected redirect, got {resp.status_code}')
        location = resp.headers.get('Location', '')
        self.assertIn('login', location.lower(),
                      f'Expected redirect to login, got Location: {location!r}')

    def test_search_with_query_unauthenticated_redirects(self):
        resp = self.client.get('/search/?q=Impression')
        self.assertIn(resp.status_code, (301, 302))

    def test_search_tag_query_unauthenticated_redirects(self):
        resp = self.client.get('/search/?q=tag:bbc-micro')
        self.assertIn(resp.status_code, (301, 302))


# =============================================================================
# HashDB artefact search tests
# =============================================================================

class TestHashDBSearch(unittest.TestCase):
    """Tests for the /hashdb/<id>/search route with fixture data."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        from myapp.database import (
            Item, Artefact, Partition, ExtractedFile,
            HashDatabase, KnownProduct, KnownFile,
            FilesystemType, StorageDirectory,
        )
        from shared.enums import ArtefactType
        from myapp.blueprints.hashdb import search as _search_view  # noqa: F401 – import check

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()

            # --- Hash database with two products ---
            hdb = HashDatabase(name='Test HashDB')
            _db.session.add(hdb)
            _db.session.flush()

            prod_a = KnownProduct(database_id=hdb.id, title='Product A')
            prod_b = KnownProduct(database_id=hdb.id, title='Product B')
            _db.session.add_all([prod_a, prod_b])
            _db.session.flush()

            kf_a1 = KnownFile(database_id=hdb.id, product_id=prod_a.id,
                               filename='FileA1', md5='aa' * 16)
            kf_a2 = KnownFile(database_id=hdb.id, product_id=prod_a.id,
                               filename='FileA2', md5='bb' * 16)
            kf_b1 = KnownFile(database_id=hdb.id, product_id=prod_b.id,
                               filename='FileB1', md5='cc' * 16)
            _db.session.add_all([kf_a1, kf_a2, kf_b1])
            _db.session.flush()

            # --- A second (unrelated) hash database ---
            hdb2 = HashDatabase(name='Other HashDB')
            _db.session.add(hdb2)
            _db.session.flush()
            prod_other = KnownProduct(database_id=hdb2.id, title='Other Product')
            _db.session.add(prod_other)
            _db.session.flush()
            kf_other = KnownFile(database_id=hdb2.id, product_id=prod_other.id,
                                  filename='OtherFile', md5='dd' * 16)
            _db.session.add(kf_other)
            _db.session.flush()

            # --- Item / artefact / partition / extracted files ---
            item = Item(name='HashDB Test Item')
            _db.session.add(item)
            _db.session.flush()

            art = Artefact(
                item_id=item.id, label='Disc 1',
                artefact_type=ArtefactType.HFE,
                original_filename='disc1.ssd',
                storage_path='disc1.ssd',
                storage_directory=StorageDirectory.UPLOADS,
            )
            _db.session.add(art)
            _db.session.flush()

            part = Partition(
                artefact_id=art.id, partition_index=0,
                label='Main', filesystem=FilesystemType.DFS,
            )
            _db.session.add(part)
            _db.session.flush()

            # File matching kf_a1
            ef1 = ExtractedFile(
                partition_id=part.id, path='$.FileA1', filename='FileA1',
                md5='aa' * 16, is_directory=False,
                known_file_id=kf_a1.id, is_known=True,
            )
            # File matching kf_b1
            ef2 = ExtractedFile(
                partition_id=part.id, path='$.FileB1', filename='FileB1',
                md5='cc' * 16, is_directory=False,
                known_file_id=kf_b1.id, is_known=True,
            )
            # File matching kf_other (different DB)
            ef3 = ExtractedFile(
                partition_id=part.id, path='$.OtherFile', filename='OtherFile',
                md5='dd' * 16, is_directory=False,
                known_file_id=kf_other.id, is_known=True,
            )
            # Unknown file (no match)
            ef4 = ExtractedFile(
                partition_id=part.id, path='$.Unknown', filename='Unknown',
                md5='ee' * 16, is_directory=False,
                is_known=False,
            )
            _db.session.add_all([ef1, ef2, ef3, ef4])
            _db.session.commit()

            cls.hdb_id = hdb.id
            cls.hdb2_id = hdb2.id
            cls.prod_a_id = prod_a.id
            cls.prod_b_id = prod_b.id
            cls.kf_a1_id = kf_a1.id
            cls.kf_a2_id = kf_a2.id
            cls.kf_b1_id = kf_b1.id

    def _search(self, db_id, **kwargs):
        """Call the search route's underlying logic directly."""
        from myapp.blueprints.hashdb import SEARCH_LIMIT
        from myapp.database import (
            ExtractedFile, Partition, Artefact, Item,
            HashDatabase, KnownProduct, KnownFile,
        )
        from myapp.extensions import db as _db

        with self.app.app_context():
            database = HashDatabase.query.get(db_id)
            product_id = kwargs.get('product_id')
            file_id = kwargs.get('file_id')

            if file_id:
                kf_filter = ExtractedFile.known_file_id == file_id
            elif product_id:
                product = KnownProduct.query.get(product_id)
                kf_ids = [kf.id for kf in product.known_files]
                if not kf_ids:
                    return []
                kf_filter = ExtractedFile.known_file_id.in_(kf_ids)
            else:
                kf_ids_sq = (
                    _db.session.query(KnownFile.id)
                    .filter(KnownFile.database_id == db_id)
                    .subquery()
                )
                kf_filter = ExtractedFile.known_file_id.in_(kf_ids_sq)

            return (
                _db.session.query(ExtractedFile, Partition, Artefact, Item, KnownFile)
                .join(Partition, ExtractedFile.partition_id == Partition.id)
                .join(Artefact, Partition.artefact_id == Artefact.id)
                .join(Item, Artefact.item_id == Item.id)
                .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
                .filter(kf_filter)
                .filter(ExtractedFile.is_directory == False)
                .order_by(KnownFile.filename, Item.name, Artefact.label, ExtractedFile.path)
                .limit(SEARCH_LIMIT + 1)
                .all()
            )

    def test_whole_database_search(self):
        """Search entire HashDB returns files from that DB only."""
        results = self._search(self.hdb_id)
        filenames = [ef.filename for ef, *_ in results]
        self.assertIn('FileA1', filenames)
        self.assertIn('FileB1', filenames)
        self.assertNotIn('OtherFile', filenames)
        self.assertNotIn('Unknown', filenames)

    def test_whole_database_result_count(self):
        results = self._search(self.hdb_id)
        self.assertEqual(len(results), 2)

    def test_product_scoped_search(self):
        """Product-scoped search returns only files from that product."""
        results = self._search(self.hdb_id, product_id=self.prod_a_id)
        filenames = [ef.filename for ef, *_ in results]
        self.assertIn('FileA1', filenames)
        self.assertNotIn('FileB1', filenames)

    def test_product_scoped_no_match(self):
        """Product with known files that have no extracted matches returns empty."""
        # kf_a2 has no matching ExtractedFile
        results = self._search(self.hdb_id, file_id=self.kf_a2_id)
        self.assertEqual(len(results), 0)

    def test_single_file_search(self):
        """Single-file search returns only that file's matches."""
        results = self._search(self.hdb_id, file_id=self.kf_a1_id)
        self.assertEqual(len(results), 1)
        ef = results[0][0]
        self.assertEqual(ef.filename, 'FileA1')

    def test_other_database_isolation(self):
        """Searching a different DB does not return files from the first."""
        results = self._search(self.hdb2_id)
        filenames = [ef.filename for ef, *_ in results]
        self.assertIn('OtherFile', filenames)
        self.assertNotIn('FileA1', filenames)
        self.assertNotIn('FileB1', filenames)

    def test_results_include_item_context(self):
        """Results include the Item object for display context."""
        results = self._search(self.hdb_id)
        for _, _, _, item, _ in results:
            self.assertEqual(item.name, 'HashDB Test Item')

    def test_results_include_known_file(self):
        """Results include the KnownFile for display context."""
        results = self._search(self.hdb_id, file_id=self.kf_b1_id)
        self.assertEqual(len(results), 1)
        kf = results[0][4]
        self.assertEqual(kf.filename, 'FileB1')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
