"""
RISC OS module parser unit tests.

Tests the decode_module() function with synthetic module binaries.
No database or Flask app context needed — pure unit tests.

Run:
    python -m unittest ci.test_riscos_module -v
"""

import os
import struct
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from worker.arcworker.tools.riscos_module import (
    HelpParseError,
    ModuleParseError,
    _parse_help_string,
    _parse_module_date_string,
    _read_string,
    decode_module,
)


def _build_module(title=b'TestModule\x00',
                  help_string=b'Test Module\t1.00 (01 Jan 2000)\x00',
                  extra_size=64):
    """Build a minimal RISC OS module binary for testing.

    The module header is 52 bytes (13 words).  Title and help strings
    are placed immediately after the header.
    """
    header_size = 52
    title_off = header_size
    help_off = title_off + len(title)
    total = help_off + len(help_string) + extra_size

    # Pad to total size
    data = bytearray(total)

    # Write header words
    header = struct.pack(
        '< IIIIIII IIII II',
        0,           # start (branch to code)
        0,           # init
        0,           # final
        0,           # service
        title_off,   # title string offset
        help_off,    # help string offset
        0,           # command table (none)
        0,           # SWI chunk (none)
        0,           # SWI handler
        0,           # SWI decoding table
        0,           # SWI decoding code
        0,           # messages file
        0,           # module flags
    )
    data[:52] = header
    data[title_off:title_off + len(title)] = title
    data[help_off:help_off + len(help_string)] = help_string
    return bytes(data)


class TestDecodeModuleBasic(unittest.TestCase):
    """Test basic decode_module functionality with synthetic binaries."""

    def test_standard_module(self):
        data = _build_module()
        result = decode_module(data)
        self.assertEqual(result['title_string'], 'TestModule')
        self.assertEqual(result['help_title'], 'Test Module')
        self.assertEqual(result['version'], '1.00')
        self.assertEqual(result['date'], '2000-01-01')
        self.assertIsNone(result['other'])
        self.assertIn('hash', result)

    def test_module_with_version_letter(self):
        data = _build_module(help_string=b'ADFS\t2.30a (15 Feb 1990)\x00')
        result = decode_module(data)
        self.assertEqual(result['help_title'], 'ADFS')
        self.assertEqual(result['version'], '2.30a')
        self.assertEqual(result['date'], '1990-02-15')

    def test_module_with_other_field(self):
        data = _build_module(help_string=b'MyModule\t1.00 (01 Jan 2000) Extra info\x00')
        result = decode_module(data)
        self.assertEqual(result['other'], 'Extra info')

    def test_module_with_tab_separated_title(self):
        data = _build_module(help_string=b'Window Manager\t2.05 (31 Jan 1990)\x00')
        result = decode_module(data)
        self.assertEqual(result['help_title'], 'Window Manager')
        self.assertEqual(result['version'], '2.05')
        self.assertEqual(result['date'], '1990-01-31')

    def test_module_too_small(self):
        with self.assertRaises(ModuleParseError):
            decode_module(b'\x00' * 20)

    def test_module_exactly_52_bytes(self):
        # 52 bytes is minimum header; title/help at offset 0 = empty strings
        data = b'\x00' * 52
        # title_string_off=0 points to start of file (the branch instruction)
        # This should work since offset 0 is valid
        result = decode_module(data)
        self.assertEqual(result['title_string'], '')

    def test_title_string_offset_beyond_data(self):
        data = bytearray(52)
        struct.pack_into('< I', data, 16, 0xFFFF)  # title string offset = 0xFFFF
        with self.assertRaises(ModuleParseError):
            decode_module(bytes(data))


class TestDecodeModuleHelpStringEdgeCases(unittest.TestCase):
    """Test modules with missing or non-standard help strings."""

    def test_no_help_string(self):
        """Module with help_string_off=0 should still parse title."""
        data = _build_module()
        # Zero out the help string offset (word at offset 20)
        data = bytearray(data)
        struct.pack_into('< I', data, 20, 0)
        data = bytes(data)
        result = decode_module(data)
        self.assertEqual(result['title_string'], 'TestModule')
        self.assertIsNone(result['help_string'])
        self.assertIsNone(result['version'])
        self.assertIsNone(result['date'])

    def test_unparseable_help_string(self):
        """Module with help string that doesn't match the version/date pattern."""
        data = _build_module(
            help_string=b'DeCompression module \xa91993 The fourth dimension\x00'
        )
        result = decode_module(data)
        self.assertEqual(result['title_string'], 'TestModule')
        # Should still extract something as help_title
        self.assertIsNotNone(result['help_title'])
        self.assertIsNone(result['version'])
        self.assertIsNone(result['date'])

    def test_help_string_with_no_date(self):
        """Help string with version but no parenthesised date."""
        data = _build_module(
            help_string=b'SomeModule 1.00\x00'
        )
        result = decode_module(data)
        self.assertEqual(result['title_string'], 'TestModule')
        # Unparseable — gracefully degrades
        self.assertIsNone(result['version'])

    def test_help_string_copyright_symbol(self):
        """Help string containing a copyright symbol (non-ASCII)."""
        data = _build_module(
            help_string=b'MyLib\t1.00 (01 Jan 2000) \xa9 Acorn\x00'
        )
        result = decode_module(data)
        self.assertEqual(result['help_title'], 'MyLib')
        self.assertEqual(result['version'], '1.00')

    def test_help_string_tab_only(self):
        """Help string with title but tab and no version."""
        data = _build_module(
            help_string=b'BadModule\tno version here\x00'
        )
        result = decode_module(data)
        # Should gracefully degrade — extract title before tab
        self.assertEqual(result['help_title'], 'BadModule')
        self.assertIsNone(result['version'])


class TestDecodeModuleSWIs(unittest.TestCase):
    """Test SWI chunk and name parsing."""

    def test_no_swi_chunk(self):
        data = _build_module()
        result = decode_module(data)
        self.assertIsNone(result['swi_chunk'])
        self.assertIsNone(result['swi_names'])

    def test_valid_swi_chunk(self):
        """Module with valid SWI chunk and decoding table."""
        title = b'SWITest\x00'
        help_str = b'SWI Test\t1.00 (01 Jan 2000)\x00'
        header_size = 52
        title_off = header_size
        help_off = title_off + len(title)
        swi_table_off = help_off + len(help_str)

        # SWI names: chunk name + individual names + terminator
        swi_data = b'Wimp\x00' + b'Initialise\x00' + b'CreateWindow\x00' + b'\x00'

        total = swi_table_off + len(swi_data) + 64
        data = bytearray(total)

        # SWI chunk must be a multiple of 64 and <= 0xFFFFFF
        swi_chunk = 0x400c0  # Wimp SWI chunk

        header = struct.pack(
            '< IIIIIII IIII II',
            0, 0, 0, 0,
            title_off,
            help_off,
            0,               # command table
            swi_chunk,       # SWI chunk
            header_size,     # SWI handler (must point within module)
            swi_table_off,   # SWI decoding table
            0,               # SWI decoding code
            0,               # messages file
            0,               # module flags
        )
        data[:52] = header
        data[title_off:title_off + len(title)] = title
        data[help_off:help_off + len(help_str)] = help_str
        data[swi_table_off:swi_table_off + len(swi_data)] = swi_data

        result = decode_module(bytes(data))
        self.assertEqual(result['swi_chunk'], swi_chunk)
        self.assertIsNotNone(result['swi_names'])
        self.assertEqual(result['swi_names'][0], 'Wimp')
        self.assertIn('Initialise', result['swi_names'])
        self.assertIn('CreateWindow', result['swi_names'])


class TestDecodeModuleCommands(unittest.TestCase):
    """Test star command table parsing."""

    def test_module_with_command(self):
        """Module with a single star command."""
        title = b'CmdTest\x00'
        help_str = b'Command Test\t1.00 (01 Jan 2000)\x00'
        header_size = 52
        title_off = header_size
        help_off = title_off + len(title)
        cmd_table_off = help_off + len(help_str)

        # Align cmd_table_off to word boundary
        cmd_table_off = (cmd_table_off + 3) & (~3)

        # Command: name + padding + 4 words (code, info, syntax, help) + terminator
        cmd_name = b'MyCommand\x00'
        # Pad to word alignment
        padded_len = (len(cmd_name) + 3) & (~3)
        cmd_entry = cmd_name + b'\x00' * (padded_len - len(cmd_name))
        # 4 words: code_offset, info_word, syntax_offset(0), help_offset(0)
        cmd_entry += struct.pack('< IIII', 0, 0, 0, 0)
        # Terminator
        cmd_entry += b'\x00'

        total = cmd_table_off + len(cmd_entry) + 64
        data = bytearray(total)

        header = struct.pack(
            '< IIIIIII IIII II',
            0, 0, 0, 0,
            title_off,
            help_off,
            cmd_table_off,  # command table
            0, 0, 0, 0,
            0, 0,
        )
        data[:52] = header
        data[title_off:title_off + len(title)] = title
        data[help_off:help_off + len(help_str)] = help_str
        data[cmd_table_off:cmd_table_off + len(cmd_entry)] = cmd_entry

        result = decode_module(bytes(data))
        self.assertIsNotNone(result['commands'])
        self.assertEqual(len(result['commands']), 1)
        self.assertEqual(result['commands'][0]['name'], 'MyCommand')


class TestParseHelpString(unittest.TestCase):
    """Unit tests for _parse_help_string."""

    def test_standard_format(self):
        title, ver, date, other = _parse_help_string('Window Manager\t2.05 (31 Jan 1990)')
        self.assertEqual(title, 'Window Manager')
        self.assertEqual(ver, '2.05')
        self.assertEqual(date, '31 Jan 1990')
        self.assertEqual(other, '')

    def test_with_version_letter(self):
        title, ver, date, other = _parse_help_string('ADFS\t2.30a (15 Feb 1990)')
        self.assertEqual(ver, '2.30a')

    def test_with_extra_info(self):
        title, ver, date, other = _parse_help_string('MyMod\t1.00 (01 Jan 2000) (c) Acorn')
        self.assertEqual(other, '(c) Acorn')

    def test_unmatched_raises(self):
        with self.assertRaises(HelpParseError):
            _parse_help_string('No version or date here')


class TestParseDateString(unittest.TestCase):
    """Unit tests for _parse_module_date_string."""

    def test_standard_date(self):
        self.assertEqual(_parse_module_date_string('01 Jan 2000'), '2000-01-01')

    def test_all_months(self):
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        for i, month in enumerate(months, 1):
            result = _parse_module_date_string(f'15 {month} 1995')
            self.assertEqual(result, f'1995-{i:02}-15')

    def test_unknown_month_raises(self):
        with self.assertRaises(HelpParseError):
            _parse_module_date_string('01 Xyz 2000')


class TestReadString(unittest.TestCase):
    """Unit tests for _read_string."""

    def test_basic_string(self):
        data = b'Hello\x00World'
        s, pos = _read_string(data, 0)
        self.assertEqual(s, 'Hello')
        self.assertEqual(pos, 5)

    def test_empty_string(self):
        data = b'\x00Rest'
        s, pos = _read_string(data, 0)
        self.assertEqual(s, '')
        self.assertEqual(pos, 0)

    def test_string_at_offset(self):
        data = b'XXXTest\x00'
        s, pos = _read_string(data, 3)
        self.assertEqual(s, 'Test')


class TestDecodeModuleHash(unittest.TestCase):
    """Test hash computation and pre-computed hash passthrough."""

    def test_hash_computed(self):
        data = _build_module()
        result = decode_module(data)
        import hashlib
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(result['hash'], expected)

    def test_hash_precomputed(self):
        data = _build_module()
        result = decode_module(data, module_hash='abc123')
        self.assertEqual(result['hash'], 'abc123')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
