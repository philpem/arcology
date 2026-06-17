"""
Tests for the `arco hashdb generate-riscos` !Run Obey parser and the
RISC OS application Mandatory/Optional classification (stdlib + cli package only).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cli.arccli.commands.hashdb_generate import (  # noqa: E402
    _product_context,
    _resolve_obey_path,
    apply_canonical_filter,
    build_product_files,
    build_product_title,
    canonical_accepts,
    classify_app_files,
    collect_canonical_candidates,
    diagnose_no_mandatory,
    get_launched_set,
    local_uniqueness_failure,
    make_is_unique,
    merge_identical_products,
    parse_artefact_label,
    parse_canonical_sources,
    parse_run_obey,
    render_canonical_candidates,
)


def _f(path, **kw):
    """Build an extracted-file dict; filename is the leaf of *path*."""
    d = {
        'path': path,
        'filename': path.rsplit('/', 1)[-1],
        'md5': kw.get('md5', 'aa' * 16),
        'sha1': kw.get('sha1'),
        'sha256': kw.get('sha256'),
        'file_size': kw.get('file_size', 100),
        'is_known': kw.get('is_known', False),
        'is_directory': kw.get('is_directory', False),
        'risc_os_filetype': kw.get('risc_os_filetype'),
        'uuid': kw.get('uuid'),
    }
    return d


class TestResolveObeyPath(unittest.TestCase):
    def test_strips_dir_variable(self):
        self.assertEqual(_resolve_obey_path('<Obey$Dir>.!RunImage'), '!RunImage')

    def test_subdirectory(self):
        self.assertEqual(_resolve_obey_path('<Obey$Dir>.bin.loader'), 'bin/loader')

    def test_unset_variable_is_external(self):
        # Strict: a variable with no Set (and not Obey$Dir) is external.
        self.assertIsNone(_resolve_obey_path('<App$Dir>.!RunImage'))

    def test_options_and_args_rejected(self):
        self.assertIsNone(_resolve_obey_path('-quit'))
        self.assertIsNone(_resolve_obey_path('%*0'))
        self.assertIsNone(_resolve_obey_path(''))

    def test_quoted(self):
        self.assertEqual(_resolve_obey_path('"<Obey$Dir>.!RunImage"'), '!RunImage')


class TestParseRunObey(unittest.TestCase):
    def test_run_absolute(self):
        text = (
            "| !Run for MyApp\n"
            "Set MyApp$Dir <Obey$Dir>\n"
            "WimpSlot -min 320k -max 320k\n"
            "Run <MyApp$Dir>.!RunImage %*0\n"
        )
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_basic_quit(self):
        text = "*BASIC -quit <Obey$Dir>.!RunImage\n"
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_basic_quit_no_star(self):
        text = "BASIC -quit <Obey$Dir>.Sources.Main\n"
        self.assertEqual(parse_run_obey(text), ['sources/main'])

    def test_rmensure_rmload(self):
        text = (
            "| Load the module first\n"
            "RMEnsure UtilityModule 0.00 RMLoad <Obey$Dir>.Modules.MyMod\n"
            "Run <Obey$Dir>.!RunImage\n"
        )
        self.assertEqual(parse_run_obey(text), ['modules/mymod', '!runimage'])

    def test_comments_and_blank_lines_ignored(self):
        text = "| comment\n\n   \n| another\n"
        self.assertEqual(parse_run_obey(text), [])

    def test_command_case_insensitive(self):
        text = "RUN <Obey$Dir>.!RunImage\n"
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_deduplicates(self):
        text = "Run <Obey$Dir>.!RunImage\nRun <Obey$Dir>.!RunImage\n"
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_set_app_dir_indirection(self):
        text = (
            "Set App$Dir <Obey$Dir>\n"
            "WimpSlot -min 256k -max 256k\n"
            "Run <App$Dir>.!RunImage\n"
        )
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_set_app_dir_subdirectory(self):
        text = (
            "Set App$Dir <Obey$Dir>.Bin\n"
            "Run <App$Dir>.loader\n"
        )
        self.assertEqual(parse_run_obey(text), ['bin/loader'])

    def test_set_chained_variables(self):
        text = (
            "Set A$Dir <Obey$Dir>\n"
            "Set B$Dir <A$Dir>.sub\n"
            "Run <B$Dir>.prog\n"
        )
        self.assertEqual(parse_run_obey(text), ['sub/prog'])

    def test_setmacro_indirection(self):
        text = (
            "SetMacro App$Dir <Obey$Dir>\n"
            "*BASIC -quit <App$Dir>.!RunImage\n"
        )
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_external_system_var_excluded(self):
        text = (
            "RMEnsure SharedMod 1.00 RMLoad <System$Dir>.Modules.SharedMod\n"
            "Run <Obey$Dir>.!RunImage\n"
        )
        # System$Dir is never Set here -> external; only !RunImage remains.
        self.assertEqual(parse_run_obey(text), ['!runimage'])

    def test_unset_variable_dropped(self):
        # Strict: a variable referenced but never Set is treated as external,
        # so its target is dropped entirely.
        text = (
            "Run <Foo$Dir>.!RunImage\n"
            "Run <Obey$Dir>.!Real\n"
        )
        self.assertEqual(parse_run_obey(text), ['!real'])

    def test_extra_vars_seed_resolves_reference(self):
        # A variable supplied via extra_vars (e.g. from !Boot) resolves.
        text = "Run <Foo$Dir>.!RunImage\n"
        self.assertEqual(
            parse_run_obey(text, extra_vars={'foo$dir': '<Obey$Dir>'}),
            ['!runimage'],
        )

    def test_var_set_to_external_literal_dropped(self):
        # App$Dir is Set, but to an external path (no Obey anchor) -> dropped.
        text = (
            "Set App$Dir Resources:$.Apps.Foo\n"
            "Run <App$Dir>.!RunImage\n"
        )
        self.assertEqual(parse_run_obey(text), [])


class TestClassifyAppFiles(unittest.TestCase):
    def _unique_always(self):
        return lambda f: not f.get('is_known')

    def test_launched_unique_is_mandatory(self):
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/!Boot'),
            _f('!Foo/!RunImage', risc_os_filetype='ff8'),
            _f('!Foo/!Sprites', risc_os_filetype='ff9'),
        ]
        result = classify_app_files('!Foo', files, {'!runimage'},
                                    self._unique_always())
        req = {f['filename']: is_req for f, is_req in result}
        self.assertTrue(req['!RunImage'])
        self.assertFalse(req['!Run'])
        self.assertFalse(req['!Boot'])
        self.assertFalse(req['!Sprites'])

    def test_run_and_boot_never_mandatory(self):
        # Even if !Run were somehow "launched", it stays Optional.
        files = [_f('!Foo/!Run'), _f('!Foo/!Boot')]
        result = classify_app_files('!Foo', files, {'!run', '!boot'},
                                    self._unique_always())
        self.assertTrue(all(not is_req for _f_, is_req in result))

    def test_shared_launched_demoted_to_optional(self):
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/!RunImage', risc_os_filetype='ff8'),
        ]
        # is_unique always False -> launched file cannot be Mandatory, and the
        # fallback heuristic is also gated by uniqueness.
        result = classify_app_files('!Foo', files, {'!runimage'},
                                    lambda f: False)
        self.assertTrue(all(not is_req for _f_, is_req in result))

    def test_fallback_filetype_when_no_run(self):
        files = [
            _f('!Foo/picture', risc_os_filetype='ff9'),     # plain sprite -> optional
            _f('!Foo/!Sprites', risc_os_filetype='ff9'),    # app sprites -> mandatory
            _f('!Foo/!RunImage', risc_os_filetype='ffb'),   # BASIC -> mandatory
        ]
        result = classify_app_files('!Foo', files, set(),
                                    self._unique_always())
        req = {f['filename']: is_req for f, is_req in result}
        self.assertTrue(req['!RunImage'])   # ffb is an executable filetype
        self.assertTrue(req['!Sprites'])    # !SpritesN matches the app-sprites rule
        self.assertFalse(req['picture'])    # plain sprite, not a Mandatory candidate

    def test_subdir_launched_match(self):
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/bin/loader', risc_os_filetype='ff8'),
        ]
        result = classify_app_files('!Foo', files, {'bin/loader'},
                                    self._unique_always())
        req = {f['path']: is_req for f, is_req in result}
        self.assertTrue(req['!Foo/bin/loader'])


class TestGetLaunchedSet(unittest.TestCase):
    class _FakeClient:
        def __init__(self, payloads):
            # payloads: dict[uuid -> bytes]
            self.payloads = payloads
            self.requested = []

        def download_extracted_file_bytes(self, uuid):
            self.requested.append(uuid)
            return self.payloads[uuid]

    def test_downloads_and_parses_run(self):
        files = [
            _f('!Foo/!Run', uuid='run-uuid'),
            _f('!Foo/!RunImage', risc_os_filetype='ff8'),
        ]
        client = self._FakeClient({'run-uuid': b"Run <Obey$Dir>.!RunImage\n"})
        launched = get_launched_set(client, files)
        self.assertEqual(launched, {'!runimage'})
        self.assertEqual(client.requested, ['run-uuid'])

    def test_no_run_file(self):
        files = [_f('!Foo/!RunImage', risc_os_filetype='ff8')]
        client = self._FakeClient({})
        self.assertEqual(get_launched_set(client, files), set())
        self.assertEqual(client.requested, [])

    def test_boot_supplies_variable_for_run(self):
        # The path variable is Set in !Boot, used (unset locally) in !Run.
        files = [
            _f('!Foo/!Boot', uuid='boot-uuid'),
            _f('!Foo/!Run', uuid='run-uuid'),
            _f('!Foo/!RunImage', risc_os_filetype='ff8'),
        ]
        client = self._FakeClient({
            'boot-uuid': b"Set Foo$Dir <Obey$Dir>\n",
            'run-uuid': b"Run <Foo$Dir>.!RunImage\n",
        })
        launched = get_launched_set(client, files)
        self.assertEqual(launched, {'!runimage'})
        self.assertIn('boot-uuid', client.requested)
        self.assertIn('run-uuid', client.requested)

    def test_run_set_overrides_boot(self):
        # Both define Foo$Dir; !Run's definition wins (it runs after !Boot).
        files = [
            _f('!Foo/!Boot', uuid='boot-uuid'),
            _f('!Foo/!Run', uuid='run-uuid'),
        ]
        client = self._FakeClient({
            'boot-uuid': b"Set Foo$Dir <Obey$Dir>.wrong\n",
            'run-uuid': b"Set Foo$Dir <Obey$Dir>.bin\nRun <Foo$Dir>.loader\n",
        })
        self.assertEqual(get_launched_set(client, files), {'bin/loader'})


class TestMakeIsUnique(unittest.TestCase):
    def test_unique_single_appkey(self):
        m = {'abc': {('item1', '!Foo')}}
        is_unique = make_is_unique(None, m, global_check=False)
        self.assertTrue(is_unique({'md5': 'ABC'}))

    def test_shared_multiple_appkeys(self):
        m = {'abc': {('item1', '!Foo'), ('item2', '!Bar')}}
        is_unique = make_is_unique(None, m, global_check=False)
        self.assertFalse(is_unique({'md5': 'abc'}))

    def test_known_file_not_unique(self):
        m = {'abc': {('item1', '!Foo')}}
        is_unique = make_is_unique(None, m, global_check=False)
        self.assertFalse(is_unique({'md5': 'abc', 'is_known': True}))

    def test_no_hash_not_unique(self):
        is_unique = make_is_unique(None, {}, global_check=False)
        self.assertFalse(is_unique({'md5': None}))

    def test_global_check_demotes_cross_item(self):
        class C:
            def hash_lookup(self, md5=None, sha1=None):
                return {'known_file': None,
                        'found_in': [{'item_id': 1}, {'item_id': 2}]}
        m = {'abc': {('item1', '!Foo')}}
        is_unique = make_is_unique(C(), m, global_check=True)
        self.assertFalse(is_unique({'md5': 'abc'}))

    def test_global_check_allows_single_item(self):
        class C:
            def hash_lookup(self, md5=None, sha1=None):
                return {'known_file': None, 'found_in': [{'item_id': 1}]}
        m = {'abc': {('item1', '!Foo')}}
        is_unique = make_is_unique(C(), m, global_check=True)
        self.assertTrue(is_unique({'md5': 'abc'}))


class TestLocalUniquenessFailure(unittest.TestCase):
    def test_known_file(self):
        self.assertEqual(
            local_uniqueness_failure({'md5': 'abc', 'is_known': True}, {}),
            'known',
        )

    def test_no_md5(self):
        self.assertEqual(local_uniqueness_failure({'md5': None}, {}), 'no-md5')

    def test_shared(self):
        m = {'abc': {('i', '!A'), ('i', '!B')}}
        self.assertEqual(local_uniqueness_failure({'md5': 'abc'}, m), 'shared')

    def test_locally_unique_returns_none(self):
        m = {'abc': {('i', '!A')}}
        self.assertIsNone(local_uniqueness_failure({'md5': 'ABC'}, m))

    def test_include_known_ignores_is_known(self):
        m = {'abc': {('i', '!A')}}
        f = {'md5': 'abc', 'is_known': True}
        self.assertEqual(local_uniqueness_failure(f, m), 'known')
        self.assertIsNone(local_uniqueness_failure(f, m, include_known=True))

    def test_include_known_still_enforces_uniqueness(self):
        # include_known drops only the is_known disqualifier, not the rest.
        m = {'abc': {('i', '!A'), ('i', '!B')}}
        self.assertEqual(
            local_uniqueness_failure({'md5': 'abc', 'is_known': True}, m,
                                     include_known=True),
            'shared')
        self.assertEqual(
            local_uniqueness_failure({'md5': None, 'is_known': True}, m,
                                     include_known=True),
            'no-md5')

    def test_include_known_makes_known_file_unique(self):
        m = {'abc': {('i', '!A')}}
        is_unique = make_is_unique(None, m, global_check=False,
                                   include_known=True)
        self.assertTrue(is_unique({'md5': 'abc', 'is_known': True}))

    def test_agrees_with_make_is_unique_local_portion(self):
        # local_uniqueness_failure is None iff make_is_unique (no global) is True.
        m = {'abc': {('i', '!A')}}
        is_unique = make_is_unique(None, m, global_check=False)
        for f in (
            {'md5': 'abc'},
            {'md5': 'abc', 'is_known': True},
            {'md5': None},
            {'md5': 'def'},  # not in map -> shared/absent
        ):
            self.assertEqual(
                local_uniqueness_failure(f, m) is None,
                is_unique(f),
                msg=f,
            )


class TestDiagnoseNoMandatory(unittest.TestCase):
    def _unique(self, md5_appkeys, global_check=False):
        # A real predicate so the carried attributes are present.
        return make_is_unique(None, md5_appkeys, global_check=global_check)

    def test_no_launch_target(self):
        # No parsed !Run target, and no executable filetype on any file.
        # (Note: !SpritesN would match the app-sprites rule, so use a plain
        # data file with a non-executable filetype here.)
        files = [_f('!Foo/!Run'), _f('!Foo/readme', risc_os_filetype='fff')]
        reason = diagnose_no_mandatory('!Foo', files, set(), self._unique({}))
        self.assertEqual(reason, 'no-launch-target')

    def test_launched_target_is_known(self):
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/!RunImage', md5='m1', risc_os_filetype='ff8', is_known=True),
        ]
        m = {'m1': {('i', '!Foo')}}
        reason = diagnose_no_mandatory('!Foo', files, {'!runimage'},
                                       self._unique(m))
        self.assertEqual(reason, 'known')

    def test_launched_target_shared(self):
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/!RunImage', md5='m1', risc_os_filetype='ff8'),
        ]
        m = {'m1': {('i', '!Foo'), ('i', '!Bar')}}
        reason = diagnose_no_mandatory('!Foo', files, {'!runimage'},
                                       self._unique(m))
        self.assertEqual(reason, 'shared')

    def test_globally_rejected_when_locally_unique(self):
        # Locally unique, global_check on -> the only way it could fail is global.
        files = [
            _f('!Foo/!Run'),
            _f('!Foo/!RunImage', md5='m1', risc_os_filetype='ff8'),
        ]
        m = {'m1': {('i', '!Foo')}}
        reason = diagnose_no_mandatory('!Foo', files, {'!runimage'},
                                       self._unique(m, global_check=True))
        self.assertEqual(reason, 'global')

    def test_fallback_filetype_candidate_known(self):
        # No !Run target parsed; a file with an executable filetype is the
        # candidate, and it is is_known.
        files = [
            _f('!Foo/!RunImage', md5='m1', risc_os_filetype='ffb', is_known=True),
        ]
        m = {'m1': {('i', '!Foo')}}
        reason = diagnose_no_mandatory('!Foo', files, set(), self._unique(m))
        self.assertEqual(reason, 'known')


class TestCanonicalSources(unittest.TestCase):
    def test_parse_basic(self):
        rules = parse_canonical_sources(
            '# comment\n\n!ArcFS    ArcFS .*\nSerialDev  SerialDev .*\n')
        self.assertEqual(set(rules), {'!arcfs', 'serialdev'})
        self.assertEqual(len(rules['!arcfs']), 1)

    def test_parse_regex_with_spaces(self):
        rules = parse_canonical_sources('!System   RISC OS 3\\.(1[0-9]|7) .*\n')
        self.assertTrue(rules['!system'][0].match('RISC OS 3.11 (Acorn)'))

    def test_parse_repeated_app_dir_ored(self):
        rules = parse_canonical_sources('!ArcFS ArcFS .*\n!ArcFS ArcFS+ .*\n')
        self.assertEqual(len(rules['!arcfs']), 2)

    def test_parse_malformed_line(self):
        with self.assertRaises(ValueError):
            parse_canonical_sources('!ArcFS\n')

    def test_parse_invalid_regex(self):
        with self.assertRaises(ValueError):
            parse_canonical_sources('!ArcFS [unterminated\n')

    def test_accepts_match(self):
        rules = parse_canonical_sources('!ArcFS ArcFS .*\n')
        # Golden artefact: accept.
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'ArcFS 0.52', 'ArcFS 0.52'),
                      True)
        # Bundled copy: reject.
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'RiscCAD 8', 'RiscCAD 8'),
                      False)
        # No rule: unaffected.
        self.assertIsNone(canonical_accepts(rules, '!Draw', 'Draw 1.0', 'Draw 1.0'))

    def test_accepts_case_insensitive_app_dir(self):
        rules = parse_canonical_sources('!arcfs ArcFS .*\n')
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'ArcFS 0.52', ''), True)

    def test_accepts_matches_either_clean_or_label(self):
        rules = parse_canonical_sources('!ArcFS ArcFS 0\\.52.*\n')
        # clean name doesn't match, but raw label does -> accept.
        self.assertIs(
            canonical_accepts(rules, '!ArcFS', 'ArcFS', 'ArcFS 0.52 (1994)'), True)

    def _gathered(self):
        # Two artefacts: the golden ArcFS distro and a *differing* (e.g. patched)
        # copy bundled in RiscCAD — distinct content so it's a real candidate.
        return [{
            'item': {'uuid': 'i'},
            'artefact_results': [
                {'clean_name': 'ArcFS 0.52', 'label': 'ArcFS 0.52',
                 'app_dirs': {'!ArcFS': [_f('!ArcFS/!RunImage', md5='arcfs_a')],
                              '!Boot': [_f('!Boot/!Run')]}},
                {'clean_name': 'RiscCAD 8', 'label': 'RiscCAD 8',
                 'app_dirs': {'!RiscCAD': [_f('!RiscCAD/!RunImage')],
                              '!ArcFS': [_f('!ArcFS/!RunImage', md5='arcfs_b')]}},
            ],
        }]

    def test_apply_filter_drops_non_canonical(self):
        gathered = self._gathered()
        rules = parse_canonical_sources('!ArcFS ArcFS .*\n')
        dropped, matched = apply_canonical_filter(gathered, rules)
        self.assertEqual(dropped, 1)            # the RiscCAD copy of !ArcFS
        self.assertEqual(matched, {'!arcfs'})
        ar0, ar1 = gathered[0]['artefact_results']
        self.assertIn('!ArcFS', ar0['app_dirs'])   # golden kept
        self.assertNotIn('!ArcFS', ar1['app_dirs'])  # bundled dropped
        self.assertIn('!RiscCAD', ar1['app_dirs'])   # unruled app untouched

    def test_apply_filter_reports_unmatched_rules(self):
        gathered = self._gathered()
        rules = parse_canonical_sources('!ArcFS ArcFS .*\n!Nonexist Foo .*\n')
        _dropped, matched = apply_canonical_filter(gathered, rules)
        self.assertEqual(sorted(set(rules) - matched), ['!nonexist'])

    def test_collect_candidates_only_multi_artefact_with_differing_content(self):
        gathered = self._gathered()
        cands = collect_canonical_candidates(gathered)
        # !ArcFS is on both artefacts with differing content; others on one each.
        self.assertEqual(set(cands), {'!ArcFS'})
        self.assertEqual(sorted(cands['!ArcFS']), ['ArcFS 0.52', 'RiscCAD 8'])

    def test_collect_candidates_skips_all_app_named_versions(self):
        # All copies named after the app (own version discs) -> keep all, no rule.
        gathered = [{
            'item': {'uuid': 'i'},
            'artefact_results': [
                {'clean_name': '65Host 1.14', 'label': '65Host 1.14',
                 'app_dirs': {'!65Host': [_f('!65Host/!RunImage', md5='a')]}},
                {'clean_name': '65Host 1.20', 'label': '65Host 1.20',
                 'app_dirs': {'!65Host': [_f('!65Host/!RunImage', md5='b')]}},
            ],
        }]
        self.assertEqual(collect_canonical_candidates(gathered), {})

    def test_collect_candidates_case_insensitive_grouping(self):
        # !ARCFS and !ArcFS are the same app (Filecore is case-insensitive).
        gathered = [{
            'item': {'uuid': 'i'},
            'artefact_results': [
                {'clean_name': 'ArcFS 0.73a', 'label': 'ArcFS 0.73a',
                 'app_dirs': {'!ARCFS': [_f('!ARCFS/!RunImage', md5='a')]}},
                {'clean_name': 'ArcFS 0.62', 'label': 'ArcFS 0.62',
                 'app_dirs': {'!ArcFS': [_f('!ArcFS/!RunImage', md5='b')]}},
                {'clean_name': 'RiscCAD 8', 'label': 'RiscCAD 8',
                 'app_dirs': {'!ArcFS': [_f('!ArcFS/!RunImage', md5='c')]}},
            ],
        }]
        cands = collect_canonical_candidates(gathered)
        # One grouped entry (the more common spelling !ArcFS), 3 occurrences,
        # listed because the RiscCAD copy isn't named after the app.
        self.assertEqual(set(cands), {'!ArcFS'})
        self.assertEqual(len(cands['!ArcFS']), 3)

    def test_collect_candidates_skips_identical_copies(self):
        # Equasor bundled identically with several products: byte-identical
        # everywhere -> merged automatically -> NOT a canonical candidate.
        gathered = [{
            'item': {'uuid': 'i'},
            'artefact_results': [
                {'clean_name': 'Equasor 1.04', 'label': 'Equasor 1.04',
                 'app_dirs': {'!Equasor': [_f('!Equasor/!RunImage', md5='eq')]}},
                {'clean_name': 'Impression Publisher 4.09',
                 'label': 'Impression Publisher 4.09',
                 'app_dirs': {'!Equasor': [_f('!Equasor/!RunImage', md5='eq')]}},
            ],
        }]
        self.assertEqual(collect_canonical_candidates(gathered), {})

    def test_render_marks_single_golden_active_others_commented(self):
        cands = {'!ArcFS': ['ArcFS 0.52', 'RiscCAD 8']}
        text = render_canonical_candidates(cands)
        # Exactly one base-prefix match -> that line is active.
        self.assertIn('\n!ArcFS    ArcFS 0\\.52\n', text)
        self.assertIn('\n#!ArcFS    RiscCAD 8\n', text)
        rules = parse_canonical_sources(text)
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'ArcFS 0.52', ''), True)
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'RiscCAD 8', ''), False)

    def test_render_keeps_all_app_named_versions_active(self):
        # Several versions of an app, all named after it -> all kept active so
        # the curator doesn't have to uncomment each by hand.
        cands = {'!65Host': ['65Host 1.14', '65Host 1.17', '65Host 1.20']}
        text = render_canonical_candidates(cands)
        active = [ln for ln in text.splitlines() if ln.startswith('!65Host')]
        self.assertEqual(len(active), 3)

    def test_render_keeps_versions_comments_bundles(self):
        # App-named copies kept; copies on unrelated products commented out.
        cands = {'!ArcFS': ['ArcFS 2 read-only 0.62', 'ArcFS 2 read-only 0.73a',
                            'Killer 2.000', 'RiscCAD 8.02']}
        text = render_canonical_candidates(cands)
        active = sorted(ln for ln in text.splitlines() if ln.startswith('!ArcFS'))
        commented = sorted(ln for ln in text.splitlines() if ln.startswith('#!ArcFS'))
        self.assertEqual(len(active), 2)     # the two ArcFS releases
        self.assertEqual(len(commented), 2)  # Killer + RiscCAD bundles
        # Round-trips: ArcFS releases accepted, bundles rejected.
        rules = parse_canonical_sources(text)
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'ArcFS 2 read-only 0.62', ''), True)
        self.assertIs(canonical_accepts(rules, '!ArcFS', 'Killer 2.000', ''), False)

    def test_render_no_base_match_all_commented(self):
        # App-dir name unrelated to product names (e.g. !Commander / 'Disc
        # Commander', !AudioCtrl / 'AudioWorks') -> nothing pre-activated.
        cands = {'!Commander': ['Disc Commander', 'Disc Commander 1.33'],
                 '!AudioCtrl': ['AudioWorks', 'AudioWorks 1.30']}
        text = render_canonical_candidates(cands)
        active = [ln for ln in text.splitlines()
                  if ln and not ln.startswith('#')]
        self.assertEqual(active, [])

    def test_render_hyphens_readable(self):
        cands = {'!BCF': ['BCF Cryptosystem, The - 25']}
        text = render_canonical_candidates(cands)
        self.assertIn('BCF Cryptosystem, The - 25', text)  # not 'The \\- 25'

    def test_render_empty(self):
        text = render_canonical_candidates({})
        self.assertIn('no ambiguous applications', text)


class TestMergeIdenticalProducts(unittest.TestCase):
    def _prod(self, app, context, hashes, required=None):
        # hashes: list of md5 strings; required: matching list of bools.
        required = required or [True] + [False] * (len(hashes) - 1)
        files = [{'md5': h, 'is_required': r}
                 for h, r in zip(hashes, required, strict=True)]
        return {'title': f'{app} - {context}', 'description': f'{app} - {context}',
                'files': files, '_app_dir': app, '_context': context}

    def test_identical_copies_merge_to_one(self):
        # Equasor, identical content, bundled with Impression releases + standalone.
        prods = [
            self._prod('!Equasor', 'Impression Publisher 4.09', ['e1', 'e2']),
            self._prod('!Equasor', 'Equasor 1.04', ['e1', 'e2']),
            self._prod('!Equasor', 'Impression Style 3.05N', ['e1', 'e2']),
        ]
        out, merged = merge_identical_products(prods)
        self.assertEqual(merged, 2)
        self.assertEqual(len(out), 1)
        # The standalone "Equasor ..." copy is chosen as the representative.
        self.assertEqual(out[0]['_context'], 'Equasor 1.04')
        self.assertIn('also in:', out[0]['description'])
        self.assertIn('Impression Publisher 4.09', out[0]['description'])

    def test_differing_content_kept_separate(self):
        prods = [
            self._prod('!Equasor', 'Equasor 1.04', ['v104']),
            self._prod('!Equasor', 'Equasor 1.10', ['v110']),
        ]
        out, merged = merge_identical_products(prods)
        self.assertEqual(merged, 0)
        self.assertEqual(len(out), 2)

    def test_different_apps_not_merged(self):
        # Same hashes but different app-dir -> not merged.
        prods = [
            self._prod('!Foo', 'Foo 1', ['h']),
            self._prod('!Bar', 'Bar 1', ['h']),
        ]
        out, merged = merge_identical_products(prods)
        self.assertEqual(merged, 0)
        self.assertEqual(len(out), 2)

    def test_required_flag_part_of_fingerprint(self):
        # Same hash but one marks it mandatory, the other optional -> distinct.
        prods = [
            self._prod('!Foo', 'Foo A', ['h'], required=[True]),
            self._prod('!Foo', 'Foo B', ['h'], required=[False]),
        ]
        out, merged = merge_identical_products(prods)
        self.assertEqual(merged, 0)
        self.assertEqual(len(out), 2)

    def test_case_insensitive_app_dir_merge(self):
        # Identical content under !ARCFS and !ArcFS -> same app -> merged.
        prods = [
            self._prod('!ARCFS', 'ArcFS 0.73a', ['h']),
            self._prod('!ArcFS', 'ArcFS 0.73a (copy)', ['h']),
        ]
        out, merged = merge_identical_products(prods)
        self.assertEqual(merged, 1)
        self.assertEqual(len(out), 1)

    def test_order_preserved(self):
        prods = [
            self._prod('!A', 'A 1', ['a']),
            self._prod('!Equasor', 'Impression Publisher 4.09', ['e']),
            self._prod('!Equasor', 'Equasor 1.04', ['e']),
            self._prod('!Z', 'Z 1', ['z']),
        ]
        out, _merged = merge_identical_products(prods)
        self.assertEqual([p['_app_dir'] for p in out], ['!A', '!Equasor', '!Z'])


class TestBuildProductFiles(unittest.TestCase):
    def test_carries_all_three_hashes(self):
        f = _f('!Foo/!RunImage', md5='m' * 32, sha1='s' * 40, sha256='h' * 64)
        entries = build_product_files([(f, True)])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['md5'], 'm' * 32)
        self.assertEqual(entries[0]['sha1'], 's' * 40)
        self.assertEqual(entries[0]['sha256'], 'h' * 64)
        self.assertTrue(entries[0]['is_required'])

    def test_omits_absent_hashes(self):
        f = _f('!Foo/x', md5='m' * 32, sha1=None, sha256=None)
        entries = build_product_files([(f, False)])
        self.assertEqual(len(entries), 1)
        self.assertNotIn('sha1', entries[0])
        self.assertNotIn('sha256', entries[0])

    def test_skips_file_with_no_hashes(self):
        f = _f('!Foo/data', md5=None, sha1=None, sha256=None)
        self.assertEqual(build_product_files([(f, False)]), [])


class TestProductTitle(unittest.TestCase):
    def test_title_with_context(self):
        self.assertEqual(
            build_product_title('!Impression', 'Impression 1.30 (Computer Concepts)'),
            '!Impression - Impression 1.30 (Computer Concepts)',
        )

    def test_title_with_disc(self):
        self.assertEqual(
            build_product_title('!Foo', 'Bar', 2),
            '!Foo - Bar (Disk 2)',
        )

    def test_title_no_context(self):
        self.assertEqual(build_product_title('!Foo'), '!Foo')


class TestProductContext(unittest.TestCase):
    def test_prefers_artefact_clean_name(self):
        # The collection item is named "Arcarc: Apps", but the product should be
        # identified by the artefact's own name.
        self.assertEqual(
            _product_context('BeebIt (FR) 0.53', 'Arcarc: Apps'),
            'BeebIt (FR) 0.53',
        )

    def test_falls_back_to_item_name(self):
        self.assertEqual(_product_context('', 'Arcarc: Apps'), 'Arcarc: Apps')
        self.assertEqual(_product_context(None, 'Arcarc: Apps'), 'Arcarc: Apps')


class TestParseArtefactLabel(unittest.TestCase):
    def test_clean_name_strips_disc_suffix(self):
        p = parse_artefact_label('BeebIt (FR) 0.53 (Disk 1 of 2)')
        self.assertEqual(p['clean_name'], 'BeebIt (FR) 0.53')
        self.assertEqual(p['disc_number'], 1)
        self.assertEqual(p['disc_total'], 2)

    def test_clean_name_without_disc(self):
        p = parse_artefact_label('BeebIt (FR) 0.53')
        self.assertEqual(p['clean_name'], 'BeebIt (FR) 0.53')
        self.assertIsNone(p['disc_number'])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
