"""Landlock enforcement, lifecycle and architectural coverage regressions."""

import ast
import errno
import gzip
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from worker.arcworker.compression import decompress_if_needed, stream_to_file
from worker.arcworker.tools import archives, base, write_sandbox
from worker.arcworker.tools.process import check_sandbox, sandboxed_process


class TestSandboxPolicy(unittest.TestCase):
    def test_old_abis_fail_closed_before_creating_rules(self):
        for abi in (0, 1, 2):
            with self.subTest(abi=abi), patch.object(write_sandbox, '_syscall', return_value=abi) as syscall:
                with self.assertRaisesRegex(OSError, 'ABI 3 or newer required'):
                    write_sandbox.restrict_writes(())
                self.assertEqual(syscall.call_count, 1)

    def test_unavailable_landlock_does_not_exec_tool(self):
        with patch.object(write_sandbox, 'restrict_writes', side_effect=OSError(errno.ENOSYS, 'unavailable')), \
             patch.object(write_sandbox.os, 'execvp') as execute:
            self.assertEqual(write_sandbox.main(['sandbox', '--', 'hostile-tool']), 127)
            execute.assert_not_called()

    def test_missing_binary_preserves_fallback_exception(self):
        with patch('worker.arcworker.tools.process.shutil.which', return_value=None):
            with self.assertRaises(FileNotFoundError):
                base.run_tool(['missing-tool'], write_dirs=())

    def test_explicit_policy_required(self):
        with self.assertRaises(TypeError):
            base.run_tool(['tool'])


class TestProcessArchitecture(unittest.TestCase):
    def test_worker_has_only_one_spawn_point(self):
        allowed = {'tools/process.py', 'tools/write_sandbox.py'}
        for path in (ROOT / 'worker/arcworker').rglob('*.py'):
            if str(path.relative_to(ROOT / 'worker/arcworker')) in allowed:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    self.assertNotIn(name, (
                        'subprocess.Popen', 'subprocess.run', 'subprocess.call',
                        'subprocess.check_call', 'subprocess.check_output', 'subprocess.getoutput',
                        'subprocess.getstatusoutput', 'os.system', 'os.popen', 'os.execvp', 'os.execv',
                        'os.posix_spawn', 'os.posix_spawnp',
                    ), f'{path}:{node.lineno} bypasses the sandbox')
                    if name in ('run_tool', 'run_tool_with_output', 'sandboxed_process'):
                        self.assertTrue(any(k.arg == 'write_dirs' for k in node.keywords),
                                        f'{path}:{node.lineno} lacks an explicit write policy')

    def test_web_and_shared_code_cannot_launch_processes(self):
        for directory in ('myapp', 'shared', 'arcology_shared'):
            for path in (ROOT / directory).rglob('*.py'):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(n.name == 'subprocess' for n in node.names), str(path))
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, 'subprocess', str(path))
                    elif isinstance(node, ast.Call):
                        self.assertNotIn(ast.unparse(node.func), (
                            'os.system', 'os.popen', 'os.execv', 'os.execvp',
                            'os.posix_spawn', 'os.posix_spawnp',
                        ), f'{path}:{node.lineno}')


class TestLandlockEnforcement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            check_sandbox()
        except (OSError, RuntimeError) as exc:
            if os.environ.get('ARCOLOGY_REQUIRE_LANDLOCK') == '1':
                raise
            raise unittest.SkipTest(f'Landlock unavailable: {exc}') from exc

    def setUp(self):
        base.clear_cancel_event()
        self.tmp = tempfile.TemporaryDirectory(prefix='sandbox-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'output'
        self.out.mkdir()
        self.victim = self.root / 'input'
        self.victim.write_text('preserve')
        self.addCleanup(base.clear_cancel_event)

    def run_python(self, code, *, write_dirs=()):
        return base.run_tool([sys.executable, '-c', code], write_dirs=write_dirs)

    def test_output_and_scratch_allowed_but_sibling_mutations_denied(self):
        (self.out / 'link').symlink_to(self.victim)
        code = f'''
import os, pathlib, tempfile
out = pathlib.Path({str(self.out)!r})
victim = pathlib.Path({str(self.victim)!r})
(out / 'inside').write_text('allowed')
with tempfile.NamedTemporaryFile() as scratch:
    scratch.write(b'allowed')
with open('/dev/null', 'wb') as null:
    null.write(b'allowed')
checks = [
    lambda: (victim.parent / 'escaped').write_text('bad'),
    lambda: victim.write_text('bad'),
    lambda: os.truncate(victim, 0),
    lambda: victim.unlink(),
    lambda: (out / 'inside').rename(victim.parent / 'renamed'),
    lambda: os.link(out / 'inside', victim.parent / 'hardlink'),
    lambda: (out / 'link').write_text('bad'),
    lambda: os.mkfifo(out / 'fifo'),
]
for check in checks:
    try: check()
    except PermissionError: pass
    else: raise SystemExit('sandbox allowed a forbidden mutation')
assert victim.read_text() == 'preserve'
print(os.environ['TMPDIR'])
'''
        result = self.run_python(code, write_dirs=(self.out,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / 'inside').read_text(), 'allowed')
        self.assertFalse(Path(result.stdout.decode().strip()).exists(), 'scratch leaked')

    def test_stdout_only_child_cannot_write_in_cwd(self):
        result = base.run_tool([
            sys.executable, '-c',
            "from pathlib import Path; Path('unexpected').write_text('bad')",
        ], cwd=str(self.out), write_dirs=())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'PermissionError', result.stderr)
        self.assertFalse((self.out / 'unexpected').exists())

    def test_multiple_output_roots(self):
        other = self.root / 'poster'
        other.mkdir()
        code = f"from pathlib import Path; Path({str(self.out / 'video')!r}).touch(); Path({str(other / 'poster')!r}).touch()"
        result = self.run_python(code, write_dirs=(self.out, other))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_descendants_inherit_policy(self):
        result = base.run_tool(['/bin/sh', '-c', f'echo bad > "{self.victim}"'], write_dirs=())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.victim.read_text(), 'preserve')

    def test_success_stops_background_descendants_before_scratch_cleanup(self):
        code = '''
import os, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, os.environ['TMPDIR'])
'''
        result = self.run_python(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid, scratch = result.stdout.decode().split()
        self.assertFalse(Path(scratch).exists())
        # A killed orphan can briefly remain a zombie pending init's reap.
        # It must not still be running after the launch context has exited.
        status = Path(f'/proc/{pid}/stat')
        deadline = time.monotonic() + 2
        while status.exists():
            try:
                state = status.read_text().split(')')[1].split()[0]
            except FileNotFoundError:
                break
            if state == 'Z':
                break
            if time.monotonic() >= deadline:
                self.fail(f'background child still running: {state}')
            time.sleep(0.01)

    def test_timeout_reaps_process_and_cleans_scratch(self):
        with sandboxed_process([sys.executable, '-c', 'import time; time.sleep(60)'], write_dirs=()) as proc:
            pid = proc.pid
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.communicate(timeout=0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(subprocess.TimeoutExpired):
            base.run_tool([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0, write_dirs=())

    def test_cancellation_stops_running_child(self):
        timer = threading.Timer(0.1, base.set_cancel_event)
        timer.start()
        try:
            with self.assertRaises(base.JobCancelledException):
                base.run_tool([sys.executable, '-c', 'import time; time.sleep(60)'], write_dirs=())
        finally:
            timer.cancel()
            timer.join()

    def test_streaming_decompression_both_launch_paths(self):
        source = self.root / 'data.gz'
        source.write_bytes(gzip.compress(b'payload'))
        result = archives.decompress_single_file(source, self.out / 'single', 'gzip')
        self.assertTrue(result['success'], result)
        self.assertEqual((self.out / 'single').read_bytes(), b'payload')
        result_path = decompress_if_needed(source, self.out)
        self.assertEqual(result_path.read_bytes(), b'payload')

    def test_streaming_size_timeout_and_cancellation_remain_enforced(self):
        for outcome in ('size_exceeded', 'timeout', 'cancelled'):
            with self.subTest(outcome=outcome):
                base.clear_cancel_event()
                code = "import os, time; os.write(1, b'123'); time.sleep(60)"
                timer = threading.Timer(0.1, base.set_cancel_event)
                if outcome == 'cancelled':
                    timer.start()
                try:
                    with sandboxed_process([sys.executable, '-c', code], write_dirs=()) as proc:
                        started = time.monotonic()
                        result, _ = stream_to_file(
                            proc, self.out / 'stream',
                            2 if outcome == 'size_exceeded' else 100,
                            0.2 if outcome == 'timeout' else 5,
                        )
                        proc.wait()
                        self.assertEqual(result, outcome)
                        self.assertLess(time.monotonic() - started, 3)
                finally:
                    timer.cancel()
                    if outcome == 'cancelled':
                        timer.join()


if __name__ == '__main__':
    unittest.main()
