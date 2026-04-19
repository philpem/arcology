"""
Unit tests for _parse_riscosarc_listing() in worker/arcworker/tools/archives.py.

The function parses riscosarc -l -v output into per-file RISC OS metadata
dicts suitable for passing to enumerate_extracted_files as inf_metadata.

Run:
    python -m unittest ci.test_riscosarc_listing -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')

from worker.arcworker.tools.archives import _parse_riscosarc_listing


def _entry(
    name: str,
    local_name: str,
    load: int,
    exec_: int,
    *,
    is_dir: bool = False,
    comptype: int = 0x82,
) -> str:
    """Build a single riscosarc -l -v entry block."""
    return (
        f'Comptype = {comptype}\n'
        f'Name {name}\n'
        f'Local name {local_name}\n'
        f'Origlen 1234\n'
        # Java prints signed 32-bit ints; convert unsigned to signed.
        f'Load {load if load < 0x80000000 else load - 0x100000000}\n'
        f'Exec {exec_ if exec_ < 0x80000000 else exec_ - 0x100000000}\n'
        f'CRC 12345\n'
        f'attr 33\n'
        f'maxbits 0\n'
        f'complen 900\n'
        f'seek 0\n'
        f'isDir {"true" if is_dir else "false"}\n'
    )


class TestParseRiscosarcListingBasic(unittest.TestCase):

    def test_empty_output(self):
        self.assertEqual(_parse_riscosarc_listing(''), {})

    def test_whitespace_only(self):
        self.assertEqual(_parse_riscosarc_listing('   \n\n   '), {})

    def test_single_date_stamped_file(self):
        # Load address format: bits 31:20 = 0xFFF (date-stamped), bits 19:8 = filetype.
        # 0xFFFFF800: bits 19:8 = (0xFFFFF800 >> 8) & 0xFFF = 0xFF8 → filetype 'ff8'.
        load = 0xFFFFF800
        exec_ = 0x23DABCA0
        listing = _entry('MyFile', 'MyFile,FF8', load, exec_)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('MyFile', result)
        meta = result['MyFile']
        self.assertEqual(meta['load_address'], f'{load:08x}')
        self.assertEqual(meta['exec_address'], f'{exec_:08x}')
        self.assertEqual(meta['risc_os_filetype'], 'ff8')
        self.assertIn('modified_time', meta)

    def test_single_non_date_stamped_file(self):
        # Load address that is NOT date-stamped (top 12 bits != 0xFFF).
        load = 0x00008000
        exec_ = 0x00009000
        listing = _entry('BBCFile', 'BBCFile', load, exec_)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('BBCFile', result)
        meta = result['BBCFile']
        self.assertEqual(meta['load_address'], f'{load:08x}')
        self.assertEqual(meta['exec_address'], f'{exec_:08x}')
        self.assertNotIn('risc_os_filetype', meta)
        self.assertNotIn('modified_time', meta)

    def test_directory_entry_skipped(self):
        listing = _entry('MyDir', 'MyDir', 0, 0, is_dir=True)
        self.assertEqual(_parse_riscosarc_listing(listing), {})

    def test_signed_load_exec_conversion(self):
        # Typical RISC OS date-stamped load address 0xFFFFFD00 is negative
        # when treated as a signed 32-bit int (-768 decimal).
        load = 0xFFFFFD00
        exec_ = 0xABCD1234
        listing = _entry('DataFile', 'DataFile,FFD', load, exec_)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('DataFile', result)
        meta = result['DataFile']
        self.assertEqual(meta['load_address'], 'fffffd00')
        self.assertEqual(meta['exec_address'], 'abcd1234')
        self.assertEqual(meta['risc_os_filetype'], 'ffd')

    def test_zeroed_load_exec(self):
        # Non-ARCHPACK Spark entry — load=0, exec=0 (Java defaults).
        listing = _entry('PlainFile', 'PlainFile,ffd', 0, 0)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('PlainFile', result)
        meta = result['PlainFile']
        self.assertEqual(meta['load_address'], '00000000')
        self.assertEqual(meta['exec_address'], '00000000')
        self.assertNotIn('modified_time', meta)


class TestParseRiscosarcListingPaths(unittest.TestCase):

    def test_subdirectory_file(self):
        # 0xFFFFEB00: bits 19:8 = (0xFFFFEB00 >> 8) & 0xFFF = 0xFEB → filetype 'feb'.
        listing = _entry('$.Dir.File', 'Dir/File,FEB', 0xFFFFEB00, 0x12345678)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('Dir/File', result)
        meta = result['Dir/File']
        self.assertEqual(meta['risc_os_filetype'], 'feb')

    def test_nested_subdirectory(self):
        listing = _entry('$.A.B.C', 'A/B/C,FFB', 0xFFFFFB00, 0xDEADBEEF)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('A/B/C', result)

    def test_no_filetype_suffix(self):
        # File with no ,xxx suffix (unusual but possible).
        listing = _entry('Plain', 'Plain', 0x00001234, 0x00005678)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('Plain', result)

    def test_double_extension_squash(self):
        # Squash member: riscosarc names it File,FCA,BBC
        # (FCA = Squash filetype, BBC = original filetype).
        # After stripping the outer suffix, display name = File.
        load = 0xFFFFFBC0
        exec_ = 0x98765432
        listing = _entry('File', 'File,FCA,BBC', load, exec_)
        result = _parse_riscosarc_listing(listing)

        # Key should be 'File' (both suffixes stripped to display name).
        self.assertIn('File', result)
        self.assertNotIn('File,FCA,BBC', result)
        self.assertNotIn('File,BBC', result)

    def test_double_extension_in_subdir(self):
        listing = _entry('$.Dir.File', 'Dir/File,FCA,BBC', 0xFFFFFBC0, 0x11111111)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('Dir/File', result)


class TestParseRiscosarcListingMultiple(unittest.TestCase):

    def test_multiple_files(self):
        load1 = 0xFFFFFFF0
        exec1 = 0x11111111
        load2 = 0xFFFFFD00
        exec2 = 0x22222222
        listing = (
            _entry('File1', 'File1,FFF', load1, exec1) +
            _entry('File2', 'File2,FFD', load2, exec2)
        )
        result = _parse_riscosarc_listing(listing)

        self.assertEqual(len(result), 2)
        self.assertIn('File1', result)
        self.assertIn('File2', result)
        self.assertEqual(result['File1']['risc_os_filetype'], 'fff')
        self.assertEqual(result['File2']['risc_os_filetype'], 'ffd')

    def test_mixed_files_and_dirs(self):
        listing = (
            _entry('MyDir', 'MyDir', 0, 0, is_dir=True) +
            _entry('MyFile', 'MyFile,FFD', 0xFFFFFD00, 0x12345678)
        )
        result = _parse_riscosarc_listing(listing)

        self.assertEqual(len(result), 1)
        self.assertIn('MyFile', result)

    def test_arcfs_style_listing(self):
        # ArcFS: all entries have load/exec, mix of date-stamped and not.
        listing = (
            _entry('$.!Run', '!Run,FEB', 0xFFFFFEB0, 0xAABBCCDD) +
            _entry('$.!Boot', '!Boot,FEB', 0xFFFFFEB0, 0x11223344) +
            _entry('$.Data', 'Data,FFD', 0xFFFFFD00, 0x55667788)
        )
        result = _parse_riscosarc_listing(listing)

        self.assertEqual(len(result), 3)
        for key in ('!Run', '!Boot', 'Data'):
            self.assertIn(key, result)
            self.assertIn('modified_time', result[key])


class TestParseRiscosarcListingModifiedTime(unittest.TestCase):

    def test_known_timestamp(self):
        # RISC OS timestamp for 2000-01-01 00:00:00 UTC:
        # unix_seconds = 946684800; cs_from_risc_os_epoch = 946684800*100 + 220898880000
        #              = 94668480000 + 220898880000 = 315567360000 cs
        # 5-byte split: high byte = 315567360000 >> 32 = 73 = 0x49
        #               low 4 bytes = 315567360000 & 0xFFFFFFFF = 2034747392 = 0x7947C800
        # For filetype 0xFFF: load bits 31:20=0xFFF, bits 19:8=0xFFF, bits 7:0=0x49
        #   → load = 0xFFFFFF49, exec = 0x7947C800
        load = 0xFFFFFF49
        exec_ = 0x7947C800
        listing = _entry('TestFile', 'TestFile,FFF', load, exec_)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('TestFile', result)
        meta = result['TestFile']
        self.assertIn('modified_time', meta)
        self.assertTrue(meta['modified_time'].startswith('2000-01-01'))

    def test_no_modified_time_for_non_date_stamped(self):
        # Load address 0x00008000: top 12 bits = 0x000, NOT 0xFFF, so not date-stamped.
        # Typical BBC Micro program load address.
        listing = _entry('BBCProg', 'BBCProg', 0x00008000, 0x00008023)
        result = _parse_riscosarc_listing(listing)

        self.assertIn('BBCProg', result)
        self.assertNotIn('risc_os_filetype', result['BBCProg'])
        self.assertNotIn('modified_time', result['BBCProg'])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
