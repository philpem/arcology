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

from myapp.extensions import db  # noqa: E402  (Query.get -> Session.get migration)


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

    def test_command_key(self):
        tokens = self.parse('command:Filer_Run')
        self.assertEqual(tokens.get('command'), ['Filer_Run'])

    def test_swi_key(self):
        tokens = self.parse('swi:ADFS_DiscOp')
        self.assertEqual(tokens.get('swi'), ['ADFS_DiscOp'])

    def test_swi_key_wildcard(self):
        tokens = self.parse('swi:OS_*')
        self.assertEqual(tokens.get('swi'), ['OS_*'])

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

    # Acorn Replay / ARMovie keys

    def test_replay_title_key(self):
        tokens = self.parse('ReplayTitle:"lion fish"')
        self.assertEqual(tokens.get('replay_title'), ['lion fish'])

    def test_replay_video_format_key(self):
        tokens = self.parse('ReplayVideoFormat:7')
        self.assertEqual(tokens.get('replay_vformat'), ['7'])

    def test_replay_video_codec_synonym(self):
        # ReplayVideoCodec and ReplayCodec are synonyms for ReplayVideoFormat.
        self.assertEqual(self.parse('ReplayVideoCodec:7').get('replay_vformat'), ['7'])
        self.assertEqual(self.parse('ReplayCodec:7').get('replay_vformat'), ['7'])

    def test_replay_width_range_value_preserved(self):
        tokens = self.parse('ReplayWidth:160..299')
        self.assertEqual(tokens.get('replay_width'), ['160..299'])

    def test_replay_duration_operator_value_preserved(self):
        tokens = self.parse('ReplayDuration:>=30')
        self.assertEqual(tokens.get('replay_duration'), ['>=30'])

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

    # Negation

    def test_negation_simple(self):
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('!type:Obey')
        self.assertEqual(tokens.get(NOT_KEY), {'type': ['Obey']})
        # Negation-only query has no positive terms
        self.assertEqual([k for k in tokens if k != NOT_KEY], [])

    def test_negation_with_positive(self):
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('type:Basic !type:Obey')
        self.assertEqual(tokens.get('type'), ['Basic'])
        self.assertEqual(tokens[NOT_KEY], {'type': ['Obey']})

    def test_negation_alias_applied(self):
        from myapp.blueprints.search import NOT_KEY
        # '!filetype:Obey' should normalise the key to 'type'
        tokens = self.parse('!filetype:Obey')
        self.assertEqual(tokens[NOT_KEY], {'type': ['Obey']})

    def test_negation_quoted_value(self):
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('!label:"Boot Disc"')
        self.assertEqual(tokens[NOT_KEY], {'label': ['Boot Disc']})

    def test_negation_not_present_without_bang(self):
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('type:Obey')
        self.assertNotIn(NOT_KEY, tokens)

    def test_bare_bang_word_is_literal_text(self):
        # A bare word starting with '!' (e.g. RISC OS '!Boot') is NOT a negation
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('!Boot')
        self.assertEqual(tokens.get('text'), ['!Boot'])
        self.assertNotIn(NOT_KEY, tokens)

    def test_negation_value_keeps_internal_bang(self):
        # The value of a positive term may itself start with '!'
        from myapp.blueprints.search import NOT_KEY
        tokens = self.parse('filename:!RunImage')
        self.assertEqual(tokens.get('filename'), ['!RunImage'])
        self.assertNotIn(NOT_KEY, tokens)

    # Unknown key detection

    def test_unknown_key_detected(self):
        from myapp.blueprints.search import KNOWN_KEYS, NOT_KEY
        tokens = self.parse('name:Dummy')
        used = (set(tokens) - {NOT_KEY}) | set(tokens.get(NOT_KEY, {}))
        self.assertTrue(used - KNOWN_KEYS, "Expected 'name' to be flagged as unknown")

    def test_known_key_not_flagged(self):
        from myapp.blueprints.search import KNOWN_KEYS, NOT_KEY
        tokens = self.parse('filename:!RunImage type:fff')
        used = (set(tokens) - {NOT_KEY}) | set(tokens.get(NOT_KEY, {}))
        self.assertEqual(used - KNOWN_KEYS, set())

    def test_alias_resolves_to_known_key(self):
        # 'file:' is an alias for 'filename:' — after resolution it must not appear unknown
        from myapp.blueprints.search import KNOWN_KEYS, NOT_KEY
        tokens = self.parse('file:!RunImage')
        used = (set(tokens) - {NOT_KEY}) | set(tokens.get(NOT_KEY, {}))
        self.assertEqual(used - KNOWN_KEYS, set())

    def test_negated_unknown_key_detected(self):
        from myapp.blueprints.search import KNOWN_KEYS, NOT_KEY
        tokens = self.parse('filename:!RunImage !name:Dummy')
        used = (set(tokens) - {NOT_KEY}) | set(tokens.get(NOT_KEY, {}))
        self.assertIn('name', used - KNOWN_KEYS)


# =============================================================================
# Unit tests: _check_query_warnings (no database required)
# =============================================================================

class TestCheckQueryWarnings(unittest.TestCase):
    """Unit tests for _check_query_warnings — no app context needed."""

    @classmethod
    def setUpClass(cls):
        from myapp.blueprints.search import _check_query_warnings, parse_query
        cls.warn = staticmethod(lambda q: _check_query_warnings(parse_query(q)))

    def _texts(self, q):
        """Return warning strings (stripped of markup) for a query string."""
        return [str(w) for w in self.warn(q)]

    # Orphaned negations

    def test_orphaned_negation_disc_without_disc_positive(self):
        # !label: with only a file positive term — disc search never runs
        warns = self._texts('filename:foo !label:System')
        self.assertTrue(any('!label:' in w for w in warns))

    def test_orphaned_negation_file_without_file_positive(self):
        # !filename: with only a disc positive term
        warns = self._texts('label:System !filename:foo')
        self.assertTrue(any('!filename:' in w for w in warns))

    def test_no_orphaned_negation_same_group(self):
        # !label: with a disc positive — fine
        warns = self._texts('label:System !label:Secret')
        self.assertFalse(any('!label:' in w for w in warns))

    def test_no_orphaned_negation_file_neg_within_file_group(self):
        # !filename: with a file positive — fine
        warns = self._texts('type:fff !filename:foo')
        self.assertFalse(any('!filename:' in w for w in warns))

    def test_orphaned_negation_protection_without_positive(self):
        warns = self._texts('filename:foo !protection:bad_crc')
        self.assertTrue(any('!protection:' in w for w in warns))

    def test_no_orphaned_negation_protection_with_positive(self):
        warns = self._texts('protection:bad_crc !protection:weak_bits')
        self.assertFalse(any('!protection:' in w for w in warns))

    def test_orphaned_negation_replay_without_positive(self):
        warns = self._texts('filename:foo !replay_title:secret')
        self.assertTrue(any('!replay_title:' in w for w in warns))

    def test_no_orphaned_negation_replay_with_positive(self):
        warns = self._texts('replay_title:Lion !replay_title:secret')
        self.assertFalse(any('!replay_title:' in w for w in warns))

    # Invalid RISC OS filetype

    def test_invalid_type_name_warns(self):
        warns = self._texts('type:NotARealType')
        self.assertTrue(any('NotARealType' in w for w in warns))

    def test_valid_type_hex_no_warn(self):
        warns = self._texts('type:fff')
        self.assertFalse(any('fff' in w and 'unknown' in w for w in warns))

    def test_valid_type_name_no_warn(self):
        warns = self._texts('type:Text')
        self.assertFalse(any('unknown' in w for w in warns))

    def test_invalid_negated_type_warns(self):
        warns = self._texts('type:fff !type:NotARealType')
        self.assertTrue(any('NotARealType' in w for w in warns))

    # Wildcards in hash searches

    def test_wildcard_in_md5_warns(self):
        warns = self._texts('md5:dead*')
        self.assertTrue(any('md5:' in w and 'dead*' in w for w in warns))

    def test_wildcard_in_sha256_warns(self):
        warns = self._texts('sha256:abc*')
        self.assertTrue(any('sha256:' in w for w in warns))

    def test_no_wildcard_no_warn(self):
        warns = self._texts('md5:' + 'd' * 32)
        self.assertFalse(any('never match' in w for w in warns))

    # Hash length / format validation

    def test_prefix_md5_no_warn(self):
        # Short hex values are valid prefix searches — must NOT warn
        warns = self._texts('md5:deadbeef')
        self.assertFalse(any('md5:deadbeef' in w for w in warns))

    def test_correct_md5_no_warn(self):
        warns = self._texts('md5:' + 'a' * 32)
        self.assertFalse(any('too long' in w or 'hexadecimal' in w for w in warns))

    def test_too_long_md5_warns(self):
        warns = self._texts('md5:' + 'a' * 33)
        self.assertTrue(any('too long' in w for w in warns))

    def test_correct_sha256_no_warn(self):
        warns = self._texts('sha256:' + 'b' * 64)
        self.assertFalse(any('too long' in w or 'hexadecimal' in w for w in warns))

    def test_nonhex_md5_warns(self):
        warns = self._texts('md5:zzzzzzzz')
        self.assertTrue(any('hexadecimal' in w for w in warns))

    def test_wildcard_hash_not_double_warned(self):
        # A wildcard hash should warn about the wildcard but NOT about length/format
        warns = self._texts('md5:dead*')
        self.assertFalse(any('hexadecimal' in w or 'too long' in w for w in warns))

    def test_no_warnings_clean_query(self):
        warns = self._texts('type:fff filename:!RunImage')
        self.assertEqual(warns, [])


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
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ArtefactMastering,
            ArtefactProtection,
            ExtractedFile,
            FilesystemType,
            Item,
            Partition,
            ReplayMovie,
            RiscosModule,
            StorageDirectory,
            Tag,
        )
        from myapp.extensions import db as _db

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
                path='!Impression/!RunImage',
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
                path='Tools/Convert/bas',
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
                path='!Impression',
                filename='!Impression',
                extension=None,
                is_directory=True,
            )
            # Module files (matched by RiscosModule.file_path)
            f_wm = ExtractedFile(
                partition_id=part.id,
                path='Modules/WindowManager',
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
                path='Modules/ADFS',
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
                file_path='Modules/WindowManager',
                commands='["IconBar_SetPriority"]',
                swi_names='["Wimp_Initialise", "Wimp_CreateWindow", "Wimp_OpenWindow"]',
            ))
            _db.session.add(RiscosModule(
                artefact_id=art.id,
                title_string='ADFS',
                help_title='ADFS',
                version='2.30',
                date='1990-02-15',
                file_path='Modules/ADFS',
                commands='["ADFS", "Back", "Bye", "Desktop_ADFS"]',
                swi_names='["ADFS_DiscOp", "ADFS_HDC", "ADFS_Drives"]',
            ))

            # Acorn Replay / ARMovie file (public artefact) + its ReplayMovie row
            f_replay = ExtractedFile(
                partition_id=part.id,
                path='Video/LionFish',
                filename='LionFish',
                extension=None,
                risc_os_filetype='ae7',
                md5='r1' + '0' * 30,
                sha1='r1' + '0' * 38,
                sha256='r1' + '0' * 62,
                is_directory=False,
            )
            _db.session.add(f_replay)
            _db.session.add(ReplayMovie(
                artefact_id=art.id,
                file_path='Video/LionFish',
                title='Lion fish in the Red Sea',
                author='BBC',
                copyright='(C) BBC',
                video_format=19,
                video_label='Super Moving Blocks',
                width=160,
                height=128,
                pixel_depth=16,
                frame_rate=12.5,
                sound_format=1,
                sound_rate=44100,
                sound_channels=1,
                number_of_chunks=15,
                duration_seconds=30.0,
            ))

            # A PRIVATE artefact with an ARMovie — must be excluded for anon.
            art_priv = Artefact(
                item_id=item.id,
                label='Private',
                artefact_type=ArtefactType.HFE,
                original_filename='priv.hfe',
                storage_path='priv.hfe',
                storage_directory=StorageDirectory.UPLOADS,
                is_private=True,
                md5='ffffeeeeddddcccc0000111122223333',
                sha256='f' * 64,
            )
            _db.session.add(art_priv)
            _db.session.flush()
            part_priv = Partition(
                artefact_id=art_priv.id,
                partition_index=0,
                label='Secret',
                filesystem=FilesystemType.ADFS,
            )
            _db.session.add(part_priv)
            _db.session.flush()
            f_priv = ExtractedFile(
                partition_id=part_priv.id,
                path='Video/Secret',
                filename='Secret',
                extension=None,
                risc_os_filetype='ae7',
                md5='r2' + '0' * 30,
                sha256='r2' + '0' * 62,
                is_directory=False,
            )
            _db.session.add(f_priv)
            _db.session.add(ReplayMovie(
                artefact_id=art_priv.id,
                file_path='Video/Secret',
                title='Secret Lion fish footage',
                video_format=7,
                width=320,
                height=256,
                frame_rate=25.0,
                duration_seconds=120.0,
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
        from myapp.blueprints.search import _run_search, parse_query
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

    def test_filename_exact_match_no_substring(self):
        # filename:!Run must NOT match !RunImage — exact match only (#449)
        results = self._search('filename:!Run')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertNotIn('!RunImage', filenames)

    def test_filename_exact_match_full_name(self):
        # filename:!RunImage should still find the exact file
        results = self._search('filename:!RunImage')
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

    def test_md5_prefix_finds_file(self):
        # An 8-character prefix should match the file whose MD5 starts with it
        results = self._search('md5:deadbeef')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_sha1_finds_file(self):
        results = self._search('sha1:cafebabe' + '0' * 32)
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('!RunImage', filenames)

    def test_sha1_prefix_finds_file(self):
        results = self._search('sha1:cafebabe')
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
    # Negation
    # ------------------------------------------------------------------

    def test_negation_excludes_matching_file(self):
        # type:ffa matches WindowManager and ADFS; exclude filename ADFS
        results = self._search('type:ffa !filename:ADFS')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('WindowManager', filenames)
        self.assertNotIn('ADFS', filenames)

    def test_negation_keeps_null_column_rows(self):
        # f2 ('bas') has a NULL risc_os_filetype — !type:Obey must not drop it
        results = self._search('ext:bas !type:Obey')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('bas', filenames)

    def test_negation_by_type(self):
        # All ffa-typed files except those also typed ffa... exclude WindowManager
        results = self._search('type:ffa !filename:WindowManager')
        filenames = [ef.filename for ef, *_ in results['files']]
        self.assertIn('ADFS', filenames)
        self.assertNotIn('WindowManager', filenames)

    def test_negation_only_returns_nothing(self):
        # A pure-negation query seeds no positive result set
        results = self._search('!type:Obey')
        self.assertEqual(results['files'], [])
        self.assertEqual(results['artefacts'], [])
        self.assertEqual(results['catalogue_items'], [])

    def test_negation_tag_row_level(self):
        # Artefact is tagged bbc-micro; excluding a non-present tag keeps it
        results = self._search('tag:bbc-micro !tag:zzz_nonexistent')
        tag_results = [r for r in results['artefacts'] if r['type'] == 'tag']
        self.assertTrue(len(tag_results) > 0)

    def test_negation_protection_excludes_matching_rows(self):
        # Excluding the same protection type leaves no matching rows
        results = self._search('protection:bad_crc !protection:bad_crc')
        prot_results = [r for r in results['artefacts'] if r['type'] == 'protection']
        self.assertEqual(prot_results, [])

    def test_negation_text_excludes_item(self):
        # 'Software' matches the item; excluding a present word removes it
        results = self._search('Software !text:classic')
        self.assertEqual(results['catalogue_items'], [])

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
        self.assertIn('Modules/WindowManager', file_paths)

    def test_module_search_wildcard(self):
        results = self._search('module:Window*')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/WindowManager', file_paths)

    def test_module_search_case_insensitive(self):
        results = self._search('module:windowmanager')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/WindowManager', file_paths)

    def test_module_search_second_module(self):
        results = self._search('module:ADFS')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    def test_module_search_by_help_title(self):
        results = self._search('module:"Window Manager"')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/WindowManager', file_paths)

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

    def test_file_metadata_icons_module(self):
        # A file-search row matching a RiscosModule should get a module icon
        # entry keyed by ExtractedFile.id (parallel to the artefact listing).
        from myapp.services.file_metadata import metadata_by_file_id
        with self.app.app_context():
            results = self._search('module:WindowManager')
            module_info, replay_info = metadata_by_file_id(results['files'])
            row = next(r for r in results['files']
                       if r[0].path == 'Modules/WindowManager')
            self.assertIn(row[0].id, module_info)
            self.assertEqual(module_info[row[0].id].title_string, 'WindowManager')
            self.assertNotIn(row[0].id, replay_info)

    def test_file_metadata_icons_replay(self):
        # A file-search row matching a ReplayMovie should get a film icon entry.
        from myapp.services.file_metadata import metadata_by_file_id
        with self.app.app_context():
            results = self._search('replay_title:Lion')
            module_info, replay_info = metadata_by_file_id(results['files'])
            row = next(r for r in results['files']
                       if r[0].path == 'Video/LionFish')
            self.assertIn(row[0].id, replay_info)
            self.assertEqual(replay_info[row[0].id].title, 'Lion fish in the Red Sea')
            self.assertNotIn(row[0].id, module_info)

    def test_file_metadata_icons_empty(self):
        from myapp.services.file_metadata import metadata_by_file_id
        with self.app.app_context():
            self.assertEqual(metadata_by_file_id([]), ({}, {}))

    def test_file_metadata_by_path(self):
        # The artefact-listing adapter keys by file_path for a single artefact.
        from myapp.services.file_metadata import metadata_by_path
        with self.app.app_context():
            module_info, replay_info = metadata_by_path([self.art_id])
            self.assertEqual(module_info['Modules/WindowManager'].title_string,
                             'WindowManager')
            self.assertEqual(replay_info['Video/LionFish'].title,
                             'Lion fish in the Red Sea')

    def test_file_metadata_by_path_empty(self):
        from myapp.services.file_metadata import metadata_by_path
        with self.app.app_context():
            self.assertEqual(metadata_by_path([]), ({}, {}))

    # ------------------------------------------------------------------
    # Command searches
    # ------------------------------------------------------------------

    def test_command_search_exact(self):
        results = self._search('command:Back')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    def test_command_search_wildcard(self):
        results = self._search('command:Desktop*')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    def test_command_search_case_insensitive(self):
        results = self._search('command:iconbar_setpriority')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/WindowManager', file_paths)

    def test_command_search_no_match(self):
        results = self._search('command:NonExistentCommand')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertEqual(file_paths, [])

    # ------------------------------------------------------------------
    # SWI searches
    # ------------------------------------------------------------------

    def test_swi_search_exact(self):
        results = self._search('swi:ADFS_DiscOp')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    def test_swi_search_wildcard(self):
        results = self._search('swi:Wimp_*')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/WindowManager', file_paths)

    def test_swi_search_case_insensitive(self):
        results = self._search('swi:adfs_discop')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    def test_swi_search_no_match(self):
        results = self._search('swi:NonExistent_SWI')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertEqual(file_paths, [])

    def test_swi_search_partial_name(self):
        """Substring match: 'DiscOp' should match 'ADFS_DiscOp'."""
        results = self._search('swi:DiscOp')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Modules/ADFS', file_paths)

    # ------------------------------------------------------------------
    # Acorn Replay / ARMovie searches
    # ------------------------------------------------------------------

    def test_replay_title_substring(self):
        results = self._search('ReplayTitle:"lion fish"')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_video_format_exact(self):
        results = self._search('ReplayVideoFormat:19')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_video_codec_synonym(self):
        results = self._search('ReplayVideoCodec:19')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_video_format_no_match(self):
        results = self._search('ReplayVideoFormat:99')
        self.assertEqual(results['files'], [])

    def test_replay_width_range(self):
        results = self._search('ReplayWidth:160..299')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_width_range_excludes(self):
        # The public movie is 160 wide; a 200.. lower bound excludes it.
        results = self._search('ReplayWidth:200..')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertNotIn('Video/LionFish', file_paths)

    def test_replay_duration_gte(self):
        results = self._search('ReplayDuration:>=30')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_duration_lt_excludes(self):
        results = self._search('ReplayDuration:<10')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertNotIn('Video/LionFish', file_paths)

    def test_replay_combined_keys_and(self):
        # Title AND format must both match the same movie.
        results = self._search('ReplayTitle:"lion fish" ReplayVideoFormat:19')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertIn('Video/LionFish', file_paths)

    def test_replay_private_excluded(self):
        # The private artefact's ARMovie also matches "lion fish" but must be
        # excluded for an anonymous searcher.
        results = self._search('ReplayTitle:"lion fish"')
        file_paths = [ef.path for ef, _, _, _ in results['files']]
        self.assertNotIn('Video/Secret', file_paths)

    # ------------------------------------------------------------------
    # _numeric_filter behaviour (exact / range / operators)
    # ------------------------------------------------------------------

    def _numeric_widths(self, val, **kwargs):
        """Return the set of ReplayMovie.width values matching _numeric_filter."""
        from myapp.blueprints.search import _numeric_filter
        from myapp.database import ReplayMovie
        from myapp.extensions import db as _db
        with self.app.app_context():
            rows = (_db.session.query(ReplayMovie.width)
                    .filter(_numeric_filter(ReplayMovie.width, val, **kwargs))
                    .all())
            return {w for (w,) in rows}

    def test_numeric_exact(self):
        self.assertEqual(self._numeric_widths('160'), {160})

    def test_numeric_gte(self):
        self.assertEqual(self._numeric_widths('>=160'), {160, 320})

    def test_numeric_gt(self):
        self.assertEqual(self._numeric_widths('>160'), {320})

    def test_numeric_lte(self):
        self.assertEqual(self._numeric_widths('<=160'), {160})

    def test_numeric_lt(self):
        self.assertEqual(self._numeric_widths('<320'), {160})

    def test_numeric_range_inclusive(self):
        self.assertEqual(self._numeric_widths('160..320'), {160, 320})

    def test_numeric_range_lower_only(self):
        self.assertEqual(self._numeric_widths('200..'), {320})

    def test_numeric_range_upper_only(self):
        self.assertEqual(self._numeric_widths('..200'), {160})

    def test_numeric_unparseable_matches_nothing(self):
        self.assertEqual(self._numeric_widths('abc'), set())

    def test_numeric_float_column(self):
        from myapp.blueprints.search import _numeric_filter
        from myapp.database import ReplayMovie
        from myapp.extensions import db as _db
        with self.app.app_context():
            rows = (_db.session.query(ReplayMovie.duration_seconds)
                    .filter(_numeric_filter(ReplayMovie.duration_seconds, '>=30', is_float=True))
                    .all())
            durations = {d for (d,) in rows}
        self.assertIn(30.0, durations)
        self.assertIn(120.0, durations)

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
        self.assertIn('has_next', results)
        self.assertEqual(results['files'], [])
        self.assertEqual(results['artefacts'], [])
        self.assertEqual(results['catalogue_items'], [])
        self.assertFalse(results['has_next'])

    def test_has_next_false_when_results_fit_one_page(self):
        # Both fixture files match; per_page=100 fits them on one page.
        from myapp.blueprints.search import _run_search, parse_query
        with self.app.app_context():
            results = _run_search(parse_query('path:!Impression path:Tools'), per_page=100)
        self.assertFalse(results['has_next'])
        self.assertEqual(len(results['files']), 2)

    def test_has_next_true_when_results_exceed_page(self):
        # Both fixture files match; per_page=1 means only one fits, has_next=True.
        from myapp.blueprints.search import _run_search, parse_query
        with self.app.app_context():
            results = _run_search(parse_query('path:!Impression path:Tools'), per_page=1)
        self.assertTrue(results['has_next'])
        self.assertEqual(len(results['files']), 1)

    def test_total_reflects_real_count_not_sentinel(self):
        # The total must be the real match count, independent of the page size,
        # so the pagination widget can show the true number of pages instead of
        # only ever "current page + 1" (the old next-page-only sentinel).
        from myapp.blueprints.search import _run_search, parse_query
        with self.app.app_context():
            full = _run_search(parse_query('path:!Impression path:Tools'), per_page=100)
            paged = _run_search(parse_query('path:!Impression path:Tools'), page=1, per_page=1)
        self.assertEqual(full['total'], 2)
        # Same query, tiny page size: total is still the real count, not page+1.
        self.assertEqual(paged['total'], 2)

    def test_total_zero_when_no_match(self):
        results = self._search('xyzzy_guaranteed_no_match_12345')
        self.assertEqual(results['total'], 0)


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
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.blueprints.hashdb import search as _search_view  # noqa: F401 – import check
        from myapp.database import (
            Artefact,
            ExtractedFile,
            FilesystemType,
            HashDatabase,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
            StorageDirectory,
        )
        from myapp.extensions import db as _db

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
                partition_id=part.id, path='FileA1', filename='FileA1',
                md5='aa' * 16, is_directory=False,
                known_file_id=kf_a1.id,
            )
            # File matching kf_b1
            ef2 = ExtractedFile(
                partition_id=part.id, path='FileB1', filename='FileB1',
                md5='cc' * 16, is_directory=False,
                known_file_id=kf_b1.id,
            )
            # File matching kf_other (different DB)
            ef3 = ExtractedFile(
                partition_id=part.id, path='OtherFile', filename='OtherFile',
                md5='dd' * 16, is_directory=False,
                known_file_id=kf_other.id,
            )
            # Unknown file (no match)
            ef4 = ExtractedFile(
                partition_id=part.id, path='Unknown', filename='Unknown',
                md5='ee' * 16, is_directory=False,
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
            Artefact,
            ExtractedFile,
            Item,
            KnownFile,
            KnownProduct,
            Partition,
        )
        from myapp.extensions import db as _db

        with self.app.app_context():
            product_id = kwargs.get('product_id')
            file_id = kwargs.get('file_id')

            if file_id:
                kf_filter = ExtractedFile.known_file_id == file_id
            elif product_id:
                product = db.session.get(KnownProduct, product_id)
                kf_ids = [kf.id for kf in product.known_files]
                if not kf_ids:
                    return []
                kf_filter = ExtractedFile.known_file_id.in_(kf_ids)
            else:
                kf_ids_sq = (
                    _db.session.query(KnownFile.id)
                    .filter(KnownFile.database_id == db_id)
                    .scalar_subquery()
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




# =============================================================================
# Multi-value pagination regression tests
# =============================================================================

class TestMultiValuePagination(unittest.TestCase):
    """Multiple values for the same key must paginate as ONE result set.

    Regression test: the per-key search functions previously applied
    offset/limit once per token value and summed the totals, so a query like
    'protection:bad_crc protection:weak_bits' returned up to 2x per_page rows
    on page 1 and skipped rows on page 2.
    """

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ArtefactProtection,
            Item,
            StorageDirectory,
        )
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()

            item = Item(name='Pagination Item')
            _db.session.add(item)
            _db.session.flush()

            # Five artefacts: three with bad_crc, two with weak_bits.
            # Labels sort A1 < A2 < ... so page boundaries are predictable.
            specs = [
                ('A1', 'bad_crc'), ('A2', 'bad_crc'), ('A3', 'bad_crc'),
                ('A4', 'weak_bits'), ('A5', 'weak_bits'),
            ]
            for label, ptype in specs:
                art = Artefact(
                    item_id=item.id,
                    label=label,
                    artefact_type=ArtefactType.HFE,
                    original_filename=f'{label}.hfe',
                    storage_path=f'{label}.hfe',
                    storage_directory=StorageDirectory.UPLOADS,
                )
                _db.session.add(art)
                _db.session.flush()
                _db.session.add(ArtefactProtection(
                    artefact_id=art.id, protection_type=ptype, track=1, side=0,
                ))
            _db.session.commit()

    def _protection_results(self, page, per_page):
        from myapp.blueprints.search import _run_search, parse_query
        with self.app.app_context():
            tokens = parse_query('protection:bad_crc protection:weak_bits')
            results = _run_search(tokens, page=page, per_page=per_page)
        return (
            [r for r in results['artefacts'] if r['type'] == 'protection'],
            results['has_next'],
        )

    def test_has_next_reflects_further_pages(self):
        # 5 total artefacts, per_page=2 → pages 1 and 2 have more, page 3 does not
        _, has_next = self._protection_results(page=1, per_page=2)
        self.assertTrue(has_next)
        _, has_next = self._protection_results(page=3, per_page=2)
        self.assertFalse(has_next)

    def test_page_size_respected_with_multiple_values(self):
        rows, _ = self._protection_results(page=1, per_page=2)
        # Previously: up to per_page rows PER token value (4 here).
        self.assertEqual(len(rows), 2)

    def test_pages_partition_results_without_skips_or_dupes(self):
        labels = []
        for page in (1, 2, 3):
            rows, _ = self._protection_results(page=page, per_page=2)
            labels.extend(r['artefact'].label for r in rows)
        self.assertEqual(sorted(labels), ['A1', 'A2', 'A3', 'A4', 'A5'])
        self.assertEqual(len(labels), len(set(labels)))

    def test_total_counts_distinct_artefacts(self):
        # 5 matching artefacts → real total is 5 (so ceil(5/2) = 3 pages),
        # regardless of which page is requested.
        from myapp.blueprints.search import _run_search, parse_query
        with self.app.app_context():
            tokens = parse_query('protection:bad_crc protection:weak_bits')
            for page in (1, 2, 3):
                results = _run_search(tokens, page=page, per_page=2)
                self.assertEqual(results['total'], 5)



if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
