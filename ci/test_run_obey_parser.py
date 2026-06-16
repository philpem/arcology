"""
Tests for the `arco hashdb generate-riscos` !Run Obey parser and the
RISC OS application Mandatory/Optional classification (stdlib + cli package only).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cli.arccli.commands.hashdb_generate import (  # noqa: E402
    _item_context,
    _resolve_obey_path,
    build_product_title,
    classify_app_files,
    get_launched_set,
    make_is_unique,
    parse_run_obey,
)


def _f(path, **kw):
    """Build an extracted-file dict; filename is the leaf of *path*."""
    d = {
        'path': path,
        'filename': path.rsplit('/', 1)[-1],
        'md5': kw.get('md5', 'aa' * 16),
        'sha1': kw.get('sha1'),
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


class TestProductTitle(unittest.TestCase):
    def test_title_with_context(self):
        self.assertEqual(
            build_product_title('!Impression', 'Impression 1.30 (Computer Concepts)'),
            '!Impression — Impression 1.30 (Computer Concepts)',
        )

    def test_title_with_disc(self):
        self.assertEqual(
            build_product_title('!Foo', 'Bar', 2),
            '!Foo — Bar (Disk 2)',
        )

    def test_title_no_context(self):
        self.assertEqual(build_product_title('!Foo'), '!Foo')

    def test_item_context_appends_version(self):
        self.assertEqual(_item_context('Impression', '1.30'), 'Impression v1.30')

    def test_item_context_no_duplicate_version(self):
        self.assertEqual(
            _item_context('Impression v1.30', '1.30'), 'Impression v1.30'
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
