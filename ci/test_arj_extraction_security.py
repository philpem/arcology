"""Security regression tests for ARJ archive extraction."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from worker.arcworker.tools.archives import extract_arj


def _listing(member: str, attributes: str = 'A') -> bytes:
    return (
        'Path = archive.arj\n'
        'Type = Arj\n'
        '----------\n'
        f'Path = {member}\n'
        f'Attributes = {attributes}\n'
    ).encode()


class ArjExtractionSecurityTests(unittest.TestCase):
    def test_rejects_unsafe_paths_before_extraction(self):
        for member in ('../escaped', '/absolute', r'C:\Windows\system.ini'):
            with self.subTest(member=member):
                listing = subprocess.CompletedProcess([], 0, _listing(member), b'')
                with tempfile.TemporaryDirectory() as tmp, \
                        patch('worker.arcworker.tools.archives.run_tool', return_value=listing), \
                        patch('worker.arcworker.tools.archives.run_tool_with_output') as extract:
                    result = extract_arj(Path(tmp) / 'input.arj', Path(tmp) / 'output')

                self.assertFalse(result['success'])
                self.assertIn('Unsafe path', result['error'])
                extract.assert_not_called()

    def test_rejects_archive_when_listing_fails(self):
        listing = subprocess.CompletedProcess([], 2, b'', b'bad archive')
        with tempfile.TemporaryDirectory() as tmp, \
                patch('worker.arcworker.tools.archives.run_tool', return_value=listing), \
                patch('worker.arcworker.tools.archives.run_tool_with_output') as extract:
            result = extract_arj(Path(tmp) / 'input.arj', Path(tmp) / 'output')

        self.assertFalse(result['success'])
        self.assertIn('could not safely list', result['error'])
        extract.assert_not_called()

    def test_rejects_link_before_extraction(self):
        listing = subprocess.CompletedProcess([], 0, _listing('link', 'lrwxrwxrwx'), b'')
        with tempfile.TemporaryDirectory() as tmp, \
                patch('worker.arcworker.tools.archives.run_tool', return_value=listing), \
                patch('worker.arcworker.tools.archives.run_tool_with_output') as extract:
            result = extract_arj(Path(tmp) / 'input.arj', Path(tmp) / 'output')

        self.assertFalse(result['success'])
        self.assertIn('Symlink entry', result['error'])
        extract.assert_not_called()

    def test_safe_archive_uses_7z_extractor(self):
        listing = subprocess.CompletedProcess([], 0, _listing('folder/file'), b'')
        extracted = subprocess.CompletedProcess([], 0, b'', b'')
        with tempfile.TemporaryDirectory() as tmp, \
                patch('worker.arcworker.tools.archives.run_tool', return_value=listing), \
                patch(
                    'worker.arcworker.tools.archives.run_tool_with_output',
                    return_value=(extracted, {}),
                ) as extract:
            output_dir = Path(tmp) / 'output'
            result = extract_arj(Path(tmp) / 'input.arj', output_dir)

        self.assertTrue(result['success'])
        command = extract.call_args.args[0]
        self.assertEqual(command[:3], ['7z', 'x', '-y'])
        self.assertIn(f'-o{output_dir}', command)


if __name__ == '__main__':
    unittest.main()
