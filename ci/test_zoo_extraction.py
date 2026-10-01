"""Security regression tests for Zoo archive extraction."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestZooExtraction(unittest.TestCase):
    def test_extractor_runs_zoo_through_write_sandbox(self):
        """Zoo uses the shared extraction runner and its confined output directory."""
        from worker.arcworker.tools import archives

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(archives, "_run_extraction_command", return_value={"success": True}) as run_extractor,
        ):
            root = Path(tmp)
            archive = root / "input.zoo"
            output_dir = root / "out"
            result = archives.extract_zoo(archive, output_dir)

        self.assertTrue(result["success"])
        command = run_extractor.call_args.kwargs["cmd"]
        self.assertEqual(command[:2], ['zoo', 'x'])
        self.assertEqual(run_extractor.call_args.kwargs['output_dir'], output_dir)


    def test_extractor_is_filesystem_confined_before_it_runs(self):
        """A hostile extractor cannot write a traversal destination."""
        from worker.arcworker.tools import archives

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_zoo = bin_dir / "zoo"
            fake_zoo.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                'Path("inside.txt").write_text("inside")\n'
                "try:\n"
                '    Path("../escaped.txt").write_text("escaped")\n'
                "except PermissionError:\n"
                "    pass\n"
                "else:\n"
                '    raise SystemExit("sandbox allowed traversal write")\n',
                encoding="utf-8",
            )
            fake_zoo.chmod(fake_zoo.stat().st_mode | stat.S_IXUSR)
            archive = root / "hostile.zoo"
            archive.touch()
            output_dir = root / "out"

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                result = archives.extract_zoo(archive, output_dir)
            finally:
                os.environ["PATH"] = old_path

            process_output = result.get("process_output", {})
            if (
                not result["success"]
                and result.get("error") == "zoo failed with exit code 127"
                and "Function not implemented" in process_output.get("stderr", "")
            ):
                self.skipTest("test environment seccomp policy blocks Landlock syscalls")
            self.assertTrue(result["success"], result)
            self.assertEqual((output_dir / "inside.txt").read_text(), "inside")
            self.assertFalse((root / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()

# vim: ts=4 sw=4 et
